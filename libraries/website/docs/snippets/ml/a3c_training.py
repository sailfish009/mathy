#!pip install gym
from mathy.cli import setup_tf_env
from mathy.agent import A3CAgent, AgentConfig
import shutil
import tempfile

model_folder = tempfile.mkdtemp()
setup_tf_env()

args = AgentConfig(
    max_eps=3,
    verbose=True,
    topics=["poly-combine"],
    model_dir=model_folder,
    num_workers=2,
    print_training=True,
)
A3CAgent(args).train()
# Comment this out to keep your model
shutil.rmtree(model_folder)
