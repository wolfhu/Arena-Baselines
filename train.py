#!/usr/bin/env python

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import argparse
import yaml

import ray
from ray.tests.cluster_utils import Cluster
from ray.tune.config_parser import make_parser
from ray.tune.result import DEFAULT_RESULTS_DIR
from ray.tune.resources import resources_to_json
from ray.tune.tune import _make_scheduler, run_experiments

from envs_layer import ArenaRllibEnv

policy_id_prefix = "policy"

EXAMPLE_USAGE = """
Training example via RLlib CLI:
    rllib train --run DQN --env CartPole-v0

Grid search example via RLlib CLI:
    rllib train -f tuned_examples/cartpole-grid-search-example.yaml

Grid search example via executable:
    ./train.py -f tuned_examples/cartpole-grid-search-example.yaml

Note that -f overrides all other trial-specific command-line options.
"""


def create_parser(parser_creator=None):

    from ray.rllib.train import create_parser as create_parser
    parser = create_parser()

    parser.add_argument(
        "--env-id", default=None, type=str, help="Env id of arena-env, only applies when set --env=arena_env.")
    parser.add_argument(
        "--is-shuffle-agents",
        action="store_true",
        help="Whether shuffle agents every episode.")
    parser.add_argument(
        "--train-mode",
        action="store_false",
        help="Whether run in train mode, with faster and smaller resulotion.")
    parser.add_argument(
        "--obs-type",
        default="visual_FP",
        type=str,
        help=(
            "type of the observation; options:"
            "vector (low-dimensional vector observation)"
            "visual_FP (first-person visual observation)"
            "visual_TP (third-person visual observation)"
            "obs1-obs2-... (combine multiple types of observations)"))
    parser.add_argument(
        "--policy-assignment",
        default="independent",
        type=str,
        help=(
            "multiagent only; how to assig policies to agents;options:"
            "independent (independent learners)"
            "self_play (one policy, only one agent is learning, the others donot explore)."))

    return parser


def run(args, parser):

    # get config as experiments
    if args.config_file:
        with open(args.config_file) as f:
            experiments = yaml.safe_load(f)

    else:
        # Note: keep this in sync with tune/config_parser.py
        experiments = {
            args.experiment_name: {  # i.e. log to ~/ray_results/default
                "run": args.run,
                "checkpoint_freq": args.checkpoint_freq,
                "keep_checkpoints_num": args.keep_checkpoints_num,
                "checkpoint_score_attr": args.checkpoint_score_attr,
                "local_dir": args.local_dir,
                "resources_per_trial": (
                    args.resources_per_trial and
                    resources_to_json(args.resources_per_trial)),
                "stop": args.stop,
                "config": dict(
                    args.config,
                    env=args.env,
                    policy_assignment=args.policy_assignment,
                    env_config=dict(
                        env_id=args.env_id,
                        is_shuffle_agents=args.is_shuffle_agents,
                        train_mode=args.train_mode,
                        obs_type=args.obs_type,
                    )
                ),
                "restore": args.restore,
                "num_samples": args.num_samples,
                "upload_dir": args.upload_dir,
            }
        }

    for exp in experiments.values():

        if not exp.get("run"):
            parser.error("the following arguments are required: --run")
        if not exp.get("env") and not exp.get("config", {}).get("env"):
            parser.error("the following arguments are required: --env")
        if args.eager:
            exp["config"]["eager"] = True

        # generate config for arena
        if exp["env"] in ["arena_env"]:

            # create dummy_env to get parameters
            dummy_env = ArenaRllibEnv(exp["config"]["env_config"])
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

            # check if there is config of policy_assignment
            if not exp.get("config", {}).get("policy_assignment"):
                parser.error(
                    "the following arguments are required: --policy_assignment")

            # config according to policy_assignment
            if exp["config"]["policy_assignment"] in ["independent"]:

                # build number_agents independent learning policies
                for agent_i in range(number_agents):
                    policies[get_policy_id(agent_i)] = (
                        None, obs_space, act_space, {})

            elif exp["config"]["policy_assignment"] in ["self_play"]:

                # build just one learning policy
                policies[get_policy_id(0)] = (None, obs_space, act_space, {})

                # build all other policies as playing policy

                # build custom_action_dist to be playing mode dist (no exploration)
                # TODO: support pytorch policy, currently only add support for tf_action_dist
                if act_space.__class__.__name__ == "Discrete":

                    from agents_layer import DeterministicCategorical
                    custom_action_dist = DeterministicCategorical

                elif act_space.__class__.__name__ == "Box":

                    from ray.rllib.models.tf.tf_action_dist import Deterministic
                    custom_action_dist = Deterministic

                else:

                    raise NotImplementedError

                # build all other policies as playing policy
                for agent_i in range(1, number_agents):
                    policies[get_policy_id(agent_i)] = (
                        None, obs_space, act_space, {"custom_action_dist": custom_action_dist})

            else:
                raise NotImplementedError

            # create a map from agent_id to policy_id
            if exp["config"]["policy_assignment"] in ["independent", "self_play"]:

                # create policy_mapping_fn that maps agent i to policy i, so called policy_mapping_fn_i2i
                agent_id_prefix = dummy_env.get_agent_id_prefix()

                def get_agent_i(agent_id):
                    return int(agent_id.split(agent_id_prefix + "_")[1])

                def policy_mapping_fn_i2i(agent_id):
                    return get_policy_id(get_agent_i(agent_id))

                # use policy_mapping_fn_i2i as policy_mapping_fn
                policy_mapping_fn = policy_mapping_fn_i2i

            else:
                raise NotImplementedError

            exp["config"]["multiagent"] = {
                "policies": policies,
                "policy_mapping_fn": ray.tune.function(policy_mapping_fn),
            }

            # del customized configs
            del exp["config"]["policy_assignment"]

    # config ray cluster
    if args.ray_num_nodes:
        cluster = Cluster()
        for _ in range(args.ray_num_nodes):
            cluster.add_node(
                num_cpus=args.ray_num_cpus or 1,
                num_gpus=args.ray_num_gpus or 0,
                object_store_memory=args.ray_object_store_memory,
                memory=args.ray_memory,
                redis_max_memory=args.ray_redis_max_memory)
        ray.init(address=cluster.redis_address)
    else:
        ray.init(
            address=args.ray_address,
            object_store_memory=args.ray_object_store_memory,
            memory=args.ray_memory,
            redis_max_memory=args.ray_redis_max_memory,
            num_cpus=args.ray_num_cpus,
            num_gpus=args.ray_num_gpus)

    # run
    run_experiments(
        experiments,
        scheduler=_make_scheduler(args),
        queue_trials=args.queue_trials,
        resume=args.resume)


if __name__ == "__main__":
    parser = create_parser()
    args = parser.parse_args()
    run(args, parser)
