import logging
import sys
import time
from datetime import datetime
from math import isclose

import tensorflow as tf
from tensorflow.python.training import training
from tensorflow_estimator.python.estimator.hooks.session_run_hook import SessionRunHook

# These are the tensors from our multi-head predictions. We fetch their current values
# in the below hooks
MATH_OUTPUT_TENSORS = {
    "policy": "policy/weighted_loss/value:0",
    "value": "value/weighted_loss/value:0",
    "node_ctrl": "node_ctrl/weighted_loss/value:0",
}


class EpochTrainerHook(SessionRunHook):
    """Train for a given number of epochs, logging training progress to the console"""

    def __init__(self, epochs, examples_count, batch_size):
        self.epochs = epochs
        self.examples_count = examples_count
        self.batch_size = batch_size

    def begin(self):
        self._step = -1
        self._start_time = time.time()

    def before_run(self, run_context):
        global MATH_OUTPUT_TENSORS

        self._step += 1

        return training.SessionRunArgs(MATH_OUTPUT_TENSORS)

    def after_run(self, run_context, run_values):
        def truncate(value):
            return float("%.3f" % (float(value)))

        steps_per_epoch = max(int(self.examples_count / self.batch_size), 1)
        total_steps = self.epochs * steps_per_epoch
        if self._step % steps_per_epoch != 0 and self._step < total_steps:
            return

        if self._step >= total_steps:
            return run_context.request_stop()

        current_epoch = int(self._step / steps_per_epoch) + 1
        current_time = time.time()
        duration = current_time - self._start_time
        self._start_time = current_time
        loss_pi = truncate(run_values.results["policy"])
        loss_v = truncate(run_values.results["value"])
        loss_ctrl = truncate(run_values.results["node_ctrl"])
        examples_per_sec = steps_per_epoch * (self.batch_size / duration)
        sec_per_batch = duration
        template = "%s: Epoch %d, loss = %.3f loss_pi = %.3f loss_v = %.3f, loss_ctrl = %.3f (%.1fex/s; %.3fs/batch)"
        args = (
            datetime.now(),
            current_epoch,
            loss_pi + loss_v + loss_ctrl,
            loss_pi,
            loss_v,
            loss_ctrl,
            examples_per_sec,
            sec_per_batch,
        )
        print(template % args)
        sys.stdout.flush()
