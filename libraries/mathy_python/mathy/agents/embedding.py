from typing import Any, Dict, Optional, Tuple, Union

import tensorflow as tf
import json
from ..core.expressions import MathTypeKeysMax
from ..state import MathyWindowObservation, ObservationFeatureIndices, MathyInputsType
from .swish_activation import swish


class MathyEmbedding(tf.keras.layers.Layer):
    def __init__(
        self,
        units: int,
        lstm_units: int,
        embedding_units: int,
        use_env_features: bool = False,
        use_node_values: bool = True,
        episode_reset_state_h: Optional[bool] = True,
        episode_reset_state_c: Optional[bool] = True,
        **kwargs,
    ):
        super(MathyEmbedding, self).__init__(**kwargs)
        self.units = units
        self.use_env_features = use_env_features
        self.use_node_values = use_node_values
        self.episode_reset_state_h = episode_reset_state_h
        self.episode_reset_state_c = episode_reset_state_c
        self.lstm_units = lstm_units
        self.init_rnn_state()
        self.embedding_units = embedding_units
        self.token_embedding = tf.keras.layers.Embedding(
            input_dim=MathTypeKeysMax,
            output_dim=self.embedding_units,
            name="nodes_embedding",
            mask_zero=True,
        )

        # +1 for the value
        # +1 for the time
        # +2 for the problem type hashes
        self.concat_size = (
            4 if self.use_env_features else 1 if self.use_node_values else 0
        )
        # if self.use_node_values:
        #     self.values_dense = tf.keras.layers.Dense(self.units, name="values_input")
        if self.use_env_features:
            self.time_dense = tf.keras.layers.Dense(self.units, name="time_input")
            self.type_dense = tf.keras.layers.Dense(self.units, name="type_input")
        self.in_dense = tf.keras.layers.Dense(
            self.units,
            # In transform gets the embeddings concatenated with the
            # floating point value at each node.
            input_shape=(None, self.embedding_units + self.concat_size),
            name="in_dense",
            activation="relu",
            kernel_initializer="he_normal",
        )
        self.output_dense = tf.keras.layers.Dense(
            self.embedding_units,
            name="out_dense",
            activation="relu",
            kernel_initializer="he_normal",
        )
        self.out_dense_norm = tf.keras.layers.LayerNormalization(name="out_dense_norm")
        self.time_lstm_norm = tf.keras.layers.LayerNormalization(name="lstm_time_norm")
        self.nodes_lstm_norm = tf.keras.layers.LayerNormalization(
            name="lstm_nodes_norm"
        )
        self.time_lstm = tf.keras.layers.LSTM(
            self.lstm_units,
            name="timestep_lstm",
            return_sequences=True,
            time_major=True,
            return_state=True,
        )
        self.nodes_lstm = tf.keras.layers.LSTM(
            self.lstm_units,
            name="nodes_lstm",
            return_sequences=True,
            time_major=False,
            return_state=False,
        )

    def init_rnn_state(self):
        """Track RNN states with variables in the graph"""
        self.state_c = tf.Variable(
            tf.zeros([1, self.lstm_units]),
            trainable=False,
            name="embedding/rnn/agent_state_c",
        )
        self.state_h = tf.Variable(
            tf.zeros([1, self.lstm_units]),
            trainable=False,
            name="embedding/rnn/agent_state_h",
        )
        # Historical state is twice the size of the RNN state because it's a
        # concatenation of the current state_h and the historical average state_h
        self.state_h_with_history = tf.Variable(
            tf.zeros([1, self.lstm_units * 2]),
            trainable=False,
            name="embedding/rnn/agent_state_h_with_history",
        )

    def reset_rnn_state(self, force: bool = False):
        """Zero out the RNN state for a new episode"""
        if self.episode_reset_state_h or force is True:
            self.state_h.assign(tf.zeros([1, self.lstm_units]))
            self.state_h_with_history.assign(tf.zeros([1, self.lstm_units * 2]))
        if self.episode_reset_state_c or force is True:
            self.state_c.assign(tf.zeros([1, self.lstm_units]))

    def call(self, features: MathyInputsType) -> tf.Tensor:
        nodes = tf.convert_to_tensor(features[ObservationFeatureIndices.nodes])
        values = tf.convert_to_tensor(features[ObservationFeatureIndices.values])
        type = tf.cast(features[ObservationFeatureIndices.type], dtype=tf.float32)
        time = tf.cast(features[ObservationFeatureIndices.time], dtype=tf.float32)
        nodes_shape = tf.shape(nodes)
        batch_size = nodes_shape[0]  # noqa
        sequence_length = nodes_shape[1]
        in_rnn_state = features[ObservationFeatureIndices.rnn_state][0]
        in_rnn_history = features[ObservationFeatureIndices.rnn_history][0]

        with tf.name_scope("prepare_inputs"):
            values = tf.expand_dims(values, axis=-1, name="values_input")
            query = self.token_embedding(nodes)

            # if self.use_node_values:
            #     values = self.values_dense(values)

            # If not using env features, only concatenate the tokens and values
            if not self.use_env_features and self.use_node_values:
                query = tf.concat([query, values], axis=-1, name="tokens_and_values")
                query.set_shape((None, None, self.embedding_units + self.concat_size))
            elif self.use_env_features:
                # Reshape the "type" information and combine it with each node in the
                # sequence so the nodes have context for the current task
                #
                # [Batch, len(Type)] => [Batch, 1, len(Type)]
                type_with_batch = type[:, tf.newaxis, :]
                # Repeat the type values for each node in the sequence
                #
                # [Batch, 1, len(Type)] => [Batch, len(Sequence), len(Type)]
                type_tiled = tf.tile(
                    type_with_batch, [1, sequence_length, 1], name="type_tiled"
                )

                # Reshape the "time" information so it has a time axis
                #
                # [Batch, 1] => [Batch, 1, 1]
                time_with_batch = time[:, tf.newaxis, :]
                # Repeat the time values for each node in the sequence
                #
                # [Batch, 1, 1] => [Batch, len(Sequence), 1]
                time_tiled = tf.tile(
                    time_with_batch, [1, sequence_length, 1], name="time_tiled"
                )
                env_inputs = [
                    query,
                    self.type_dense(type_tiled),
                    self.time_dense(time_tiled),
                ]
                if self.use_node_values:
                    env_inputs.insert(1, values)

                query = tf.concat(env_inputs, axis=-1, name="tokens_and_values",)
                # query.set_shape((None, None, self.embedding_units + self.concat_size))

        # Input dense transforms
        query = self.in_dense(query)

        with tf.name_scope("prepare_initial_states"):
            time_initial_h = tf.tile(
                in_rnn_state[0][0], [sequence_length, 1], name="time_hidden"
            )
            time_initial_c = tf.tile(
                in_rnn_state[1][0], [sequence_length, 1], name="time_cell"
            )

        with tf.name_scope("rnn"):
            query = self.nodes_lstm(query)
            query = self.nodes_lstm_norm(query)
            query, state_h, state_c = self.time_lstm(
                query, initial_state=[time_initial_h, time_initial_c]
            )
            query = self.time_lstm_norm(query)
            historical_state_h = tf.squeeze(
                tf.concat(in_rnn_history[0], axis=0, name="average_history_hidden"),
                axis=1,
            )

        self.state_h.assign(state_h[-1:])
        self.state_c.assign(state_c[-1:])

        # Concatenate the RNN output state with our historical RNN state
        # See: https://arxiv.org/pdf/1810.04437.pdf
        with tf.name_scope("combine_outputs"):
            rnn_state_with_history = tf.concat(
                [state_h[-1:], historical_state_h[-1:]], axis=-1,
            )
            self.state_h_with_history.assign(rnn_state_with_history)
            lstm_context = tf.tile(
                tf.expand_dims(rnn_state_with_history, axis=0),
                [batch_size, sequence_length, 1],
            )
            # Combine the output LSTM states with the historical average LSTM states
            # and concatenate it with the query
            output = tf.concat([query, lstm_context], axis=-1, name="combined_outputs")

        return self.out_dense_norm(self.output_dense(output))
