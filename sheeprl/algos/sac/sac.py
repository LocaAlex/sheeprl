import os
import warnings
from math import prod
from typing import Optional

import gymnasium as gym
import hydra
import numpy as np
import torch
from lightning.fabric import Fabric
from lightning.fabric.fabric import _is_using_cli
from lightning.fabric.plugins.collectives.collective import CollectibleGroup
from omegaconf import DictConfig, OmegaConf
from tensordict import TensorDict, make_tensordict
from tensordict.tensordict import TensorDictBase
from torch.optim import Optimizer
from torch.utils.data.distributed import DistributedSampler
from torch.utils.data.sampler import BatchSampler
from torchmetrics import MeanMetric, SumMetric

from sheeprl.algos.sac.agent import SACActor, SACAgent, SACCritic
from sheeprl.algos.sac.loss import critic_loss, entropy_loss, policy_loss
from sheeprl.algos.sac.utils import test
from sheeprl.data.buffers import ReplayBuffer
from sheeprl.utils.callback import CheckpointCallback
from sheeprl.utils.env import make_env
from sheeprl.utils.logger import create_tensorboard_logger
from sheeprl.utils.metric import MetricAggregator
from sheeprl.utils.registry import register_algorithm
from sheeprl.utils.timer import timer
from sheeprl.utils.utils import print_config


def train(
    fabric: Fabric,
    agent: SACAgent,
    actor_optimizer: Optimizer,
    qf_optimizer: Optimizer,
    alpha_optimizer: Optimizer,
    data: TensorDictBase,
    aggregator: MetricAggregator,
    update: int,
    cfg: DictConfig,
    policy_steps_per_update: int,
    group: Optional[CollectibleGroup] = None,
):
    # Update the soft-critic
    next_target_qf_value = agent.get_next_target_q_values(
        data["next_observations"],
        data["rewards"],
        data["dones"],
        cfg.algo.gamma,
    )
    qf_values = agent.get_q_values(data["observations"], data["actions"])
    qf_loss = critic_loss(qf_values, next_target_qf_value, agent.num_critics)
    qf_optimizer.zero_grad(set_to_none=True)
    fabric.backward(qf_loss)
    qf_optimizer.step()
    aggregator.update("Loss/value_loss", qf_loss)

    # Update the target networks with EMA
    if update % (cfg.algo.critic.target_network_frequency // policy_steps_per_update + 1) == 0:
        agent.qfs_target_ema()

    # Update the actor
    actions, logprobs = agent.get_actions_and_log_probs(data["observations"])
    qf_values = agent.get_q_values(data["observations"], actions)
    min_qf_values = torch.min(qf_values, dim=-1, keepdim=True)[0]
    actor_loss = policy_loss(agent.alpha, logprobs, min_qf_values)
    actor_optimizer.zero_grad(set_to_none=True)
    fabric.backward(actor_loss)
    actor_optimizer.step()
    aggregator.update("Loss/policy_loss", actor_loss)

    # Update the entropy value
    alpha_loss = entropy_loss(agent.log_alpha, logprobs.detach(), agent.target_entropy)
    alpha_optimizer.zero_grad(set_to_none=True)
    fabric.backward(alpha_loss)
    agent.log_alpha.grad = fabric.all_reduce(agent.log_alpha.grad, group=group)
    alpha_optimizer.step()
    aggregator.update("Loss/alpha_loss", alpha_loss)


@register_algorithm()
@hydra.main(version_base=None, config_path="../../configs", config_name="config")
def main(cfg: DictConfig):
    print_config(cfg)

    # Initialize Fabric
    fabric = Fabric(callbacks=[CheckpointCallback()])
    if not _is_using_cli():
        fabric.launch()
    device = fabric.device
    rank = fabric.global_rank
    world_size = fabric.world_size
    fabric.seed_everything(cfg.seed)
    torch.backends.cudnn.deterministic = cfg.torch_deterministic

    # Create TensorBoardLogger. This will create the logger only on the
    # rank-0 process
    logger, log_dir = create_tensorboard_logger(fabric, cfg, "sac")
    if fabric.is_global_zero:
        fabric._loggers = [logger]
        fabric.logger.log_hyperparams(OmegaConf.to_container(cfg, resolve=True))

    # Environment setup
    vectorized_env = gym.vector.SyncVectorEnv if cfg.env.sync_env else gym.vector.AsyncVectorEnv
    envs = vectorized_env(
        [
            make_env(
                cfg.env.id,
                cfg.seed + rank * cfg.env.num_envs + i,
                rank,
                cfg.env.capture_video,
                logger.log_dir if rank == 0 else None,
                "train",
                mask_velocities=False,
                vector_env_idx=i,
            )
            for i in range(cfg.env.num_envs)
        ]
    )
    if not isinstance(envs.single_action_space, gym.spaces.Box):
        raise ValueError("Only continuous action space is supported for the SAC agent")
    if len(envs.single_observation_space.shape) > 1:
        raise ValueError(
            "Only environments with vector-only observations are supported by the SAC agent. "
            f"Provided environment: {cfg.env.id}"
        )

    # Define the agent and the optimizer and setup them with Fabric
    act_dim = prod(envs.single_action_space.shape)
    obs_dim = prod(envs.single_observation_space.shape)
    actor = fabric.setup_module(
        SACActor(
            observation_dim=obs_dim,
            action_dim=act_dim,
            hidden_size=cfg.algo.actor.hidden_size,
            action_low=envs.single_action_space.low,
            action_high=envs.single_action_space.high,
        )
    )
    critics = [
        fabric.setup_module(
            SACCritic(observation_dim=obs_dim + act_dim, hidden_size=cfg.algo.critic.hidden_size, num_critics=1)
        )
        for _ in range(cfg.algo.critic.n)
    ]
    target_entropy = -act_dim
    agent = SACAgent(actor, critics, target_entropy, alpha=cfg.algo.alpha.alpha, tau=cfg.algo.tau, device=fabric.device)

    # Optimizers
    qf_optimizer, actor_optimizer, alpha_optimizer = fabric.setup_optimizers(
        hydra.utils.instantiate(cfg.algo.critic.optimizer, params=agent.qfs.parameters()),
        hydra.utils.instantiate(cfg.algo.actor.optimizer, params=agent.actor.parameters()),
        hydra.utils.instantiate(cfg.algo.alpha.optimizer, params=[agent.log_alpha]),
    )

    # Create a metric aggregator to log the metrics
    aggregator = MetricAggregator(
        {
            "Rewards/rew_avg": MeanMetric(sync_on_compute=cfg.metric.sync_on_compute),
            "Game/ep_len_avg": MeanMetric(sync_on_compute=cfg.metric.sync_on_compute),
            "Loss/value_loss": MeanMetric(sync_on_compute=cfg.metric.sync_on_compute),
            "Loss/policy_loss": MeanMetric(sync_on_compute=cfg.metric.sync_on_compute),
            "Loss/alpha_loss": MeanMetric(sync_on_compute=cfg.metric.sync_on_compute),
        }
    ).to(device)

    # Local data
    buffer_size = cfg.buffer.size // int(cfg.env.num_envs * world_size) if not cfg.dry_run else 1
    rb = ReplayBuffer(
        buffer_size,
        cfg.env.num_envs,
        device=device,
        memmap=cfg.buffer.memmap,
        memmap_dir=os.path.join(log_dir, "memmap_buffer", f"rank_{fabric.global_rank}"),
    )
    step_data = TensorDict({}, batch_size=[cfg.env.num_envs], device=device)

    # Global variables
    last_log = 0
    last_train = 0
    train_step = 0
    policy_step = 0
    last_checkpoint = 0
    policy_steps_per_update = int(cfg.env.num_envs * world_size)
    num_updates = int(cfg.total_steps // policy_steps_per_update) if not cfg.dry_run else 1
    learning_starts = cfg.algo.learning_starts // policy_steps_per_update if not cfg.dry_run else 0

    # Warning for log and checkpoint every
    if cfg.metric.log_every % policy_steps_per_update != 0:
        warnings.warn(
            f"The metric.log_every parameter ({cfg.metric.log_every}) is not a multiple of the "
            f"policy_steps_per_update value ({policy_steps_per_update}), so "
            "the metrics will be logged at the nearest greater multiple of the "
            "policy_steps_per_update value."
        )
    if cfg.checkpoint.every % policy_steps_per_update != 0:
        warnings.warn(
            f"The checkpoint.every parameter ({cfg.checkpoint.every}) is not a multiple of the "
            f"policy_steps_per_update value ({policy_steps_per_update}), so "
            "the checkpoint will be saved at the nearest greater multiple of the "
            "policy_steps_per_update value."
        )

    # Get the first environment observation and start the optimization
    obs = torch.tensor(envs.reset(seed=cfg.seed)[0], dtype=torch.float32, device=device)  # [N_envs, N_obs]

    for update in range(1, num_updates + 1):
        policy_step += cfg.env.num_envs * world_size

        # Measure environment interaction time: this considers both the model forward
        # to get the action given the observation and the time taken into the environment
        with timer("Time/env_interaction_time", SumMetric(sync_on_compute=cfg.metric.sync_on_compute)):
            if update <= learning_starts:
                actions = envs.action_space.sample()
            else:
                # Sample an action given the observation received by the environment
                with torch.no_grad():
                    actions, _ = actor.module(obs)
                    actions = actions.cpu().numpy()
            next_obs, rewards, dones, truncated, infos = envs.step(actions)
            dones = np.logical_or(dones, truncated)

        if "final_info" in infos:
            for i, agent_ep_info in enumerate(infos["final_info"]):
                if agent_ep_info is not None:
                    ep_rew = agent_ep_info["episode"]["r"]
                    ep_len = agent_ep_info["episode"]["l"]
                    aggregator.update("Rewards/rew_avg", ep_rew)
                    aggregator.update("Game/ep_len_avg", ep_len)
                    fabric.print(f"Rank-0: policy_step={policy_step}, reward_env_{i}={ep_rew[-1]}")

        # Save the real next observation
        real_next_obs = next_obs.copy()
        if "final_observation" in infos:
            for idx, final_obs in enumerate(infos["final_observation"]):
                if final_obs is not None:
                    real_next_obs[idx] = final_obs

        with device:
            next_obs = torch.tensor(next_obs, dtype=torch.float32)
            real_next_obs = torch.tensor(real_next_obs, dtype=torch.float32)
            actions = torch.tensor(actions, dtype=torch.float32).view(cfg.env.num_envs, -1)
            rewards = torch.tensor(rewards, dtype=torch.float32).view(cfg.env.num_envs, -1)
            dones = torch.tensor(dones, dtype=torch.float32).view(cfg.env.num_envs, -1)

        step_data["dones"] = dones
        step_data["actions"] = actions
        step_data["observations"] = obs
        if not cfg.buffer.sample_next_obs:
            step_data["next_observations"] = real_next_obs
        step_data["rewards"] = rewards
        rb.add(step_data.unsqueeze(0))

        # next_obs becomes the new obs
        obs = next_obs

        # Train the agent
        if update >= learning_starts:
            training_steps = learning_starts if update == learning_starts else 1

            # We sample one time to reduce the communications between processes
            sample = rb.sample(
                training_steps * cfg.algo.gradient_steps * cfg.per_rank_batch_size,
                sample_next_obs=cfg.buffer.sample_next_obs,
            )  # [G*B, 1]
            gathered_data = fabric.all_gather(sample.to_dict())  # [G*B, World, 1]
            gathered_data = make_tensordict(gathered_data).view(-1)  # [G*B*World]
            if world_size > 1:
                dist_sampler: DistributedSampler = DistributedSampler(
                    range(len(gathered_data)),
                    num_replicas=world_size,
                    rank=fabric.global_rank,
                    shuffle=True,
                    seed=cfg.seed,
                    drop_last=False,
                )
                sampler: BatchSampler = BatchSampler(
                    sampler=dist_sampler, batch_size=cfg.per_rank_batch_size, drop_last=False
                )
            else:
                sampler = BatchSampler(
                    sampler=range(len(gathered_data)), batch_size=cfg.per_rank_batch_size, drop_last=False
                )

            # Start training
            with timer("Time/train_time", SumMetric(sync_on_compute=cfg.metric.sync_on_compute)):
                for batch_idxes in sampler:
                    train(
                        fabric,
                        agent,
                        actor_optimizer,
                        qf_optimizer,
                        alpha_optimizer,
                        gathered_data[batch_idxes],
                        aggregator,
                        update,
                        cfg,
                        policy_steps_per_update,
                    )
                train_step += 1

        # Log metrics
        if policy_step - last_log >= cfg.metric.log_every or update == num_updates or cfg.dry_run:
            # Sync distributed metrics
            metrics_dict = aggregator.compute()
            fabric.log_dict(metrics_dict, policy_step)
            aggregator.reset()

            # Sync distributed timers
            timer_metrics = timer.compute()
            fabric.log(
                "Time/sps_train",
                (train_step - last_train) / timer_metrics["Time/train_time"],
                policy_step,
            )
            fabric.log(
                "Time/sps_env_interaction",
                ((policy_step - last_log) / world_size * cfg.env.action_repeat)
                / timer_metrics["Time/env_interaction_time"],
                policy_step,
            )
            timer.reset()

            # Reset counters
            last_log = policy_step
            last_train = train_step

        # Checkpoint model
        if (
            (cfg.checkpoint.every > 0 and policy_step - last_checkpoint >= cfg.checkpoint.every)
            or cfg.dry_run
            or update == num_updates
        ):
            last_checkpoint = policy_step
            state = {
                "agent": agent.state_dict(),
                "qf_optimizer": qf_optimizer.state_dict(),
                "actor_optimizer": actor_optimizer.state_dict(),
                "alpha_optimizer": alpha_optimizer.state_dict(),
                "update": update,
            }
            ckpt_path = os.path.join(log_dir, f"checkpoint/ckpt_{policy_step}_{fabric.global_rank}.ckpt")
            fabric.call(
                "on_checkpoint_coupled",
                fabric=fabric,
                ckpt_path=ckpt_path,
                state=state,
                replay_buffer=rb if cfg.buffer.checkpoint else None,
            )

    envs.close()
    if fabric.is_global_zero:
        test_env = make_env(
            cfg.env.id,
            None,
            0,
            cfg.env.capture_video,
            fabric.logger.log_dir,
            "test",
            mask_velocities=False,
            vector_env_idx=0,
        )()
        test(actor.module, test_env, fabric, cfg)


if __name__ == "__main__":
    main()
