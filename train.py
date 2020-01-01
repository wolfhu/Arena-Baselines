#!/usr/bin/env python

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import argparse
import yaml
import utils
import ray
from ray.tests.cluster_utils import Cluster
from ray.tune.resources import resources_to_json
from ray.tune.tune import _make_scheduler, run_experiments

import arena

POLICY_ID_PREFIX = "policy"
SELFPLAY_POLICY_TO_TRAIN = 0


def create_parser():
    """Returns parser with additional arena configs.
    """

    from ray.rllib.train import create_parser as create_parser
    parser = create_parser()

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
            "multiagent only; how to assign policies to agents; options:"
            "independent (independent learners)"
            "self_play (only one agent is learning, the others [1] donot explore or [2] update, but they keep [3] sync weights from the learning policy)."))

    return parser


def run(args, parser):

    # get config as experiments
    if args.config_file:
        with open(args.config_file) as f:
            experiments = yaml.safe_load(f)

    else:
        input("# WARNING: it is recommended to use -f CONFIG.yaml, instead of passing args. Press enter to continue.")
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
                    env_config=dict(
                        is_shuffle_agents=args.is_shuffle_agents,
                        train_mode=args.train_mode,
                        obs_type=args.obs_type,
                    ),
                    policy_assignment=args.policy_assignment,
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
        if arena.is_all_arena_env(exp["env"]):

            # create dummy_env to get parameters
            dummy_env = arena.ArenaRllibEnv(
                env=arena.get_one_from_grid_search(
                    arena.remove_arena_env_prefix(
                        exp["env"]
                    )
                ),
                env_config=exp["config"]["env_config"],
            )
            number_agents = dummy_env.number_agents

            # For now, we do not support using different spaces across agents
            # (i.e., all agents have to share the same brain in Arena-BuildingToolkit)
            # This is because we want to consider the transfer/sharing weight between agents.
            # If you do have completely different agents in game, one harmless work around is
            # to use the same brain, but define different meaning of the action in Arena-BuildingToolkit
            obs_space = dummy_env.observation_space
            act_space = dummy_env.action_space

            def get_policy_id(policy_i):
                return "{}_{}".format(POLICY_ID_PREFIX, policy_i)

            # create config of policies according to policy_assignment
            policies = None

            if exp["config"]["policy_assignment"] in ["independent"]:

                # build number_agents independent learning policies
                policies = {}
                for agent_i in range(number_agents):
                    policies[get_policy_id(agent_i)] = (
                        None, obs_space, act_space, {})

            elif exp["config"]["policy_assignment"] in ["self_play"]:

                print("# WARNING: Testing.....")

                policies = {}

                # build just one learning policy
                policies[get_policy_id(SELFPLAY_POLICY_TO_TRAIN)] = (
                    None, obs_space, act_space, {}
                )

                # build all other policies as playing policy

                # build custom_action_dist to be playing mode dist (no exploration)
                # TODO: support pytorch policy and other algorithms, currently only add support for tf_action_dist on PPO
                # see this issue for a fix: https://github.com/ray-project/ray/issues/5729

                if exp["run"] not in ["PPO"]:
                    raise NotImplementedError

                if act_space.__class__.__name__ == "Discrete":

                    from ray.rllib.models.tf.tf_action_dist import Categorical
                    from ray.rllib.utils.annotations import override

                    class DeterministicCategorical(Categorical):
                        """Deterministic version of categorical distribution for discrete action spaces."""

                        @override(Categorical)
                        def _build_sample_op(self):
                            return tf.squeeze(tf.argmax(self.inputs, 1), axis=1)

                    custom_action_dist = DeterministicCategorical

                elif act_space.__class__.__name__ == "Box":

                    from ray.rllib.models.tf.tf_action_dist import Deterministic
                    custom_action_dist = Deterministic

                else:

                    raise NotImplementedError

                # build all other policies as playing policy
                for agent_i in range(1, number_agents):
                    policies[get_policy_id(agent_i)] = (
                        None, obs_space, act_space, {
                            "custom_action_dist": custom_action_dist
                        }
                    )

            else:
                raise NotImplementedError

            # create policy_mapping_fn (a map from agent_id to policy_id) according to policy_assignment
            policy_mapping_fn = None

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

            # create policies_to_train according to policy_assignment
            policies_to_train = None

            if exp["config"]["policy_assignment"] in ["independent"]:

                # for independent policy_assignment, all policies are trained
                policies_to_train = list(policies.keys())

            elif exp["config"]["policy_assignment"] in ["self_play"]:

                # for self_play policy_assignment, only get_policy_id(0) are trained
                policies_to_train = [get_policy_id(SELFPLAY_POLICY_TO_TRAIN)]

                input("# TODO: load learning agent")

            else:
                raise NotImplementedError

            # generate multiagent part of the config
            exp["config"]["multiagent"] = {}

            if policies is not None:
                exp["config"]["multiagent"]["policies"] = policies

            if policy_mapping_fn is not None:
                exp["config"]["multiagent"]["policy_mapping_fn"] = ray.tune.function(
                    policy_mapping_fn
                )

            if policies_to_train is not None:
                exp["config"]["multiagent"]["policies_to_train"] = policies_to_train

            # del customized configs, as these configs have been reflected on other configs
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
        resume=args.resume,
    )


if __name__ == "__main__":
    parser = create_parser()
    args = parser.parse_args()
    run(args, parser)
