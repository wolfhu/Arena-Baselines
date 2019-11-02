from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
"""Simple example of using Multi-Agent and Hierarchical
(https://ray.readthedocs.io/en/latest/rllib-env.html#multi-agent-and-hierarchical)
from rllib to train an arena environment in ArenaRllibEnv.
"""

import argparse
import random
import time

import numpy as np

import ray
from ray import tune
from ray.rllib.utils import try_import_tf

from envs_layer import ArenaRllibEnv

tf = try_import_tf()

parser = argparse.ArgumentParser()

parser.add_argument("--env-id", type=str,
                    default="Tennis-Sparse-2T1P-Discrete")
parser.add_argument("--policy-assignment", type=str, default="independent")
parser.add_argument("--num-iters", type=int, default=1000)

policy_id_prefix = "policy"

if __name__ == "__main__":
    args = parser.parse_args()
    ray.init()

    env_config = {
        "env_id": args.env_id,
    }

    dummy_env = ArenaRllibEnv(env_config)
    number_agents = dummy_env.number_agents

    # For now, we do not support using different spaces across agents
    # (i.e., all agents have to share the same brain in Arena-BuildingToolkit)
    # This is because we want to consider the transfer/sharing weight between agents.
    # If you do have completely different agents in game, one harmless work around is
    # to use the same brain, but define different meaning of the action in Arena-BuildingToolkit
    obs_space = dummy_env.observation_space
    act_space = dummy_env.action_space

    def get_policy_id(policy_i):
        return "{}_{}".format(policy_id_prefix, policy_i)

    # create config of policies
    policies = {}
    for agent_i in range(number_agents):
        policy_id = get_policy_id(agent_i)
        policies[policy_id] = (None, obs_space, act_space, {})

    # create a map from agent_id to policy_id
    agent_id_to_policy_id = {}

    if args.policy_assignment in ["independent"]:
        # independent learners, each agent is assigned with a independent policy
        for agent_i in range(number_agents):
            agent_id = dummy_env.get_agent_id(agent_i)
            policy_id = get_policy_id(agent_i)
            agent_id_to_policy_id[agent_id] = policy_id
    else:
        raise NotImplementedError

    # check if all agent_id are covered in agent_id_to_policy_id
    for agent_id in dummy_env.get_agent_ids():
        if agent_id not in agent_id_to_policy_id.keys():
            raise Exception("All agent_id has to be mentioned in agent_id_to_policy_id.keys(). \
                agent_id of {} is not mentioned".format(agent_id))

    tune.run(
        "PPO",
        stop={"training_iteration": args.num_iters},
        config={
            "env": "arena_env",
            "env_config": env_config,
            "multiagent": {
                "policies": policies,
                "policy_mapping_fn": (
                    lambda agent_id: agent_id_to_policy_id[agent_id]
                ),
            },
            # === Execution ===
            # Number of environments to evaluate vectorwise per worker.
            "num_envs_per_worker": 10,

            # === Resources ===
            # Number of actors used for parallelism
            "num_workers": 1,
            # Number of GPUs to allocate to the trainer process. Note that not all
            # algorithms can take advantage of trainer GPUs. This can be fractional
            # (e.g., 0.3 GPUs).
            "num_gpus": 1,
            # Number of CPUs to allocate per worker.
            "num_cpus_per_worker": 10,
            # Number of GPUs to allocate per worker. This can be fractional.
            "num_gpus_per_worker": 1,
            # Number of CPUs to allocate for the trainer. Note: this only takes effect
            # when running in Tune.
            "num_cpus_for_driver": 2,
        },
    )
