import os
import sys
import time
import datetime
import subprocess
import numpy as np
import torch
import torch.nn as nn
import torch.optim as optim
from torch.distributions import Categorical
import traci
from tqdm import tqdm
import matplotlib.pyplot as plt
import matplotlib
# ========== 配置区 ==========
class Config:
    # SUMO配置
    sumo_binary = "sumo"  # 如果已设置SUMO_HOME，可直接使用"sumo"
    config_path = "a.sumocfg"
    ego_vehicle_id = "drl_ego_car"
    port_range = (8890, 8900)

    # 训练参数
    episodes = 1000
    max_steps = 2000
    gamma = 0.99
    clip_epsilon = 0.1
    learning_rate = 1e-4
    batch_size = 256
    ppo_epochs = 3       # 每次更新时的迭代轮数
    hidden_size = 256    # 降低隐藏层规模以稳定训练
    log_interval = 10

    # 状态和动作维度
    state_dim = 10
    action_dim = 3  # 0: 保持, 1: 左变, 2: 右变

# ========== SUMO环境封装 ==========
class SumoEnv:
    def __init__(self):
        self.current_port = Config.port_range[0]
        self.sumo_process = None
        self.change_lane_count = 0
        self.collision_count = 0
        self.current_step = 0

    def _init_sumo_cmd(self, port):
        return [
            Config.sumo_binary,
            "-c", Config.config_path,
            "--remote-port", str(port),
            "--no-warnings", "true",
            "--collision.action", "none",
            "--time-to-teleport", "-1",
            "--random"
        ]

    def reset(self):
        self._close()
        self._start_sumo()
        self._add_ego_vehicle()
        self.change_lane_count = 0
        self.collision_count = 0
        self.current_step = 0
        return self._get_state()

    def _start_sumo(self):
        for port in range(*Config.port_range):
            try:
                sumo_cmd = self._init_sumo_cmd(port)
                print(f"尝试连接SUMO，端口：{port}...")
                self.sumo_process = subprocess.Popen(sumo_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                time.sleep(2)
                traci.init(port)
                print(f"✅ SUMO连接成功，端口：{port}")
                self.current_port = port
                return
            except traci.exceptions.TraCIException:
                print(f"端口{port}连接失败，尝试下一个端口...")
                self._kill_sumo_processes()
                time.sleep(1)
        raise ConnectionError("无法在指定端口范围内连接SUMO！")

    def _add_ego_vehicle(self):
        if "ego_route" not in traci.route.getIDList():
            traci.route.add("ego_route", ["E0"])
        traci.vehicle.addFull(
            Config.ego_vehicle_id, "ego_route",
            typeID="car", depart="now",
            departLane="best", departSpeed="max"
        )
        for _ in range(20):
            traci.simulationStep()
            if Config.ego_vehicle_id in traci.vehicle.getIDList():
                return
        raise RuntimeError("自车生成失败！")

    def _get_state(self):
        state = np.zeros(Config.state_dim, dtype=np.float32)
        if Config.ego_vehicle_id not in traci.vehicle.getIDList():
            return state
        try:
            speed = traci.vehicle.getSpeed(Config.ego_vehicle_id)
            lane = traci.vehicle.getLaneIndex(Config.ego_vehicle_id)
            state[0] = speed / 33.33
            state[1] = lane / 2.0  # 归一化车道信息：0, 0.5, 1 分别对应车道 0,1,2
            self._update_surrounding_vehicles(state)
            state[8] = state[1]   # 当前车道
            state[9] = 1.0 if lane == 1 else 0.0  # 目标车道：暂设中间车道为优先
        except traci.TraCIException:
            pass
        return state

    def _update_surrounding_vehicles(self, state):
        ego_pos = traci.vehicle.getPosition(Config.ego_vehicle_id)
        ego_lane = traci.vehicle.getLaneIndex(Config.ego_vehicle_id)
        ranges = {
            'front': 100.0, 'back': 100.0,
            'left_front': 100.0, 'left_back': 100.0,
            'right_front': 100.0, 'right_back': 100.0
        }
        for veh_id in traci.vehicle.getIDList():
            if veh_id == Config.ego_vehicle_id:
                continue
            veh_lane = traci.vehicle.getLaneIndex(veh_id)
            veh_pos = traci.vehicle.getPosition(veh_id)
            dx = veh_pos[0] - ego_pos[0]
            dy = veh_pos[1] - ego_pos[1]
            distance = np.hypot(dx, dy)
            if veh_lane == ego_lane:
                if dx > 0:
                    ranges['front'] = min(ranges['front'], distance)
                else:
                    ranges['back'] = min(ranges['back'], distance)
            elif veh_lane == ego_lane - 1:
                if dx > 0:
                    ranges['left_front'] = min(ranges['left_front'], distance)
                else:
                    ranges['left_back'] = min(ranges['left_back'], distance)
            elif veh_lane == ego_lane + 1:
                if dx > 0:
                    ranges['right_front'] = min(ranges['right_front'], distance)
                else:
                    ranges['right_back'] = min(ranges['right_back'], distance)
        state[2] = ranges['front'] / 100.0
        state[3] = ranges['back'] / 100.0
        state[4] = ranges['left_front'] / 100.0
        state[5] = ranges['left_back'] / 100.0
        state[6] = ranges['right_front'] / 100.0
        state[7] = ranges['right_back'] / 100.0

    def step(self, action):
        done = False
        reward = 0.0
        try:
            lane = traci.vehicle.getLaneIndex(Config.ego_vehicle_id)
            if action == 1 and lane > 0:
                traci.vehicle.changeLane(Config.ego_vehicle_id, lane - 1, duration=2)
                self.change_lane_count += 1
            elif action == 2 and lane < 2:
                traci.vehicle.changeLane(Config.ego_vehicle_id, lane + 1, duration=2)
                self.change_lane_count += 1

            traci.simulationStep()
            reward = self._calculate_reward(action)
            self.current_step += 1
        except traci.TraCIException:
            done = True

        next_state = self._get_state()
        if traci.simulation.getTime() > 3600 or self.current_step >= Config.max_steps:
            done = True
        return next_state, reward, done

    def _calculate_reward(self, action):
        collisions = traci.simulation.getCollisions()
        if collisions:
            for collision in collisions:
                if collision.collider == Config.ego_vehicle_id or collision.victim == Config.ego_vehicle_id:
                    self.collision_count += 1
                    return -50.0  # 碰撞惩罚

        speed = traci.vehicle.getSpeed(Config.ego_vehicle_id)
        speed_reward = (speed / 33.33) * 0.5
        lane = traci.vehicle.getLaneIndex(Config.ego_vehicle_id)
        lane_reward = (2 - abs(lane - 1)) * 0.3  # 优先中间车道
        change_lane_bonus = 0.1 if action != 0 else 0.0

        ego_state = self._get_state()
        front_dist_norm = ego_state[2] * 100  # 恢复实际距离
        safe_distance_penalty = -1.0 if front_dist_norm < 5.0 else 0.0

        return speed_reward + lane_reward + change_lane_bonus + safe_distance_penalty

    def _close(self):
        if self.sumo_process:
            try:
                traci.close()
            except traci.exceptions.FatalTraCIError:
                pass
            finally:
                self.sumo_process.terminate()
                self.sumo_process.wait()
                self.sumo_process = None
                self._kill_sumo_processes()

    @staticmethod
    def _kill_sumo_processes():
        if os.name == 'nt':
            os.system("taskkill /f /im sumo.exe >nul 2>&1")
            os.system("taskkill /f /im sumo-gui.exe >nul 2>&1")

# ========== PPO算法实现 ==========
class PPO(nn.Module):
    def __init__(self):
        super(PPO, self).__init__()
        self.actor = nn.Sequential(
            nn.Linear(Config.state_dim, Config.hidden_size),
            nn.ReLU(),
            nn.Linear(Config.hidden_size, Config.hidden_size),
            nn.ReLU(),
            nn.Linear(Config.hidden_size, Config.action_dim),
            nn.Softmax(dim=-1)
        )
        self.critic = nn.Sequential(
            nn.Linear(Config.state_dim, Config.hidden_size),
            nn.ReLU(),
            nn.Linear(Config.hidden_size, Config.hidden_size),
            nn.ReLU(),
            nn.Linear(Config.hidden_size, 1)
        )

    def forward(self, x):
        return self.actor(x), self.critic(x)

# ========== Agent ==========
class Agent:
    def __init__(self):
        self.policy = PPO()
        self.optimizer = optim.Adam(self.policy.parameters(), lr=Config.learning_rate)
        self.memory = []
        self.actor_losses = []
        self.critic_losses = []
        self.total_losses = []

    def get_action(self, state):
        state_tensor = torch.FloatTensor(state)
        probs, _ = self.policy(state_tensor)
        # 动作屏蔽：确保最左侧禁止左变，最右侧禁止右变
        lane = int(state[1] * 2)
        mask = [1.0] * Config.action_dim
        if lane == 0:
            mask[1] = 0.0
        elif lane == 2:
            mask[2] = 0.0
        mask_tensor = torch.tensor(mask)
        probs = probs * mask_tensor
        probs = probs / (probs.sum() + 1e-8)  # 重新归一化
        dist = Categorical(probs)
        action = dist.sample()
        return action.item(), dist.log_prob(action)

    def store(self, transition):
        # transition = (state, action, log_prob, reward)
        self.memory.append(transition)

    def update(self):
        if len(self.memory) < Config.batch_size:
            return

        states = torch.FloatTensor([m[0] for m in self.memory])
        actions = torch.LongTensor([m[1] for m in self.memory])
        old_log_probs = torch.FloatTensor([m[2] for m in self.memory])
        rewards = torch.FloatTensor([m[3] for m in self.memory])

        # 计算折扣回报
        discounted_rewards = []
        running = 0
        for r in reversed(rewards.numpy()):
            running = r + Config.gamma * running
            discounted_rewards.insert(0, running)
        discounted_rewards = torch.FloatTensor(discounted_rewards)
        discounted_rewards = (discounted_rewards - discounted_rewards.mean()) / (discounted_rewards.std() + 1e-7)

        for _ in range(Config.ppo_epochs):
            new_probs, values = self.policy(states)
            # 根据状态中的车道信息重新应用动作屏蔽
            lanes = (states[:, 1] * 2).long()  # 0, 1, 2
            mask = torch.ones_like(new_probs)
            mask[lanes == 0, 1] = 0.0  # 若车道为0，左变非法
            mask[lanes == 2, 2] = 0.0  # 若车道为2，右变非法

            new_probs = new_probs * mask
            new_probs_sum = new_probs.sum(dim=1, keepdim=True)
            new_probs = new_probs / (new_probs_sum + 1e-8)

            dist = Categorical(new_probs)
            new_log_probs = dist.log_prob(actions)

            ratio = torch.exp(new_log_probs - old_log_probs)
            advantages = discounted_rewards - values.squeeze().detach()

            surr1 = ratio * advantages
            surr2 = torch.clamp(ratio, 1 - Config.clip_epsilon, 1 + Config.clip_epsilon) * advantages

            actor_loss = -torch.min(surr1, surr2).mean()
            critic_loss = nn.MSELoss()(values.squeeze(), discounted_rewards)
            entropy_bonus = dist.entropy().mean()
            loss = actor_loss - 0.01 * entropy_bonus + 0.5 * critic_loss

            self.optimizer.zero_grad()
            loss.backward()
            torch.nn.utils.clip_grad_norm_(self.policy.parameters(), 0.5)
            self.optimizer.step()

            self.actor_losses.append(actor_loss.item())
            self.critic_losses.append(critic_loss.item())
            self.total_losses.append(loss.item())

        self.memory.clear()

# ========== 训练主循环 ==========
def main():
    timestamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
    results_dir = f"ppo_results_{timestamp}"
    models_dir = os.path.join(results_dir, "models")
    os.makedirs(models_dir, exist_ok=True)

    env = SumoEnv()
    agent = Agent()
    best_reward = -float('inf')

    all_rewards = []
    lane_change_counts = []
    collision_counts = []
    total_steps_per_episode = []

    try:
        for episode in tqdm(range(1, Config.episodes + 1), desc="训练回合"):
            state = env.reset()
            episode_reward = 0
            done = False
            step_count = 0

            while not done and step_count < Config.max_steps:
                action, log_prob = agent.get_action(state)
                next_state, reward, done = env.step(action)
                agent.store((state, action, log_prob.item(), reward))
                state = next_state
                episode_reward += reward
                step_count += 1

            agent.update()

            all_rewards.append(episode_reward)
            lane_change_counts.append(env.change_lane_count)
            collision_counts.append(env.collision_count)
            total_steps_per_episode.append(step_count)

            if episode_reward > best_reward:
                best_reward = episode_reward
                torch.save(agent.policy.state_dict(), os.path.join(models_dir, "best_model.pth"))
                print(f"🎉 Episode {episode}, 新最佳模型！回合奖励：{best_reward:.2f}")

            if episode % Config.log_interval == 0:
                print(f"[Episode {episode}] Reward: {episode_reward:.2f}, Best: {best_reward:.2f}, "
                      f"LaneChange: {env.change_lane_count}, Collisions: {env.collision_count}")

    except KeyboardInterrupt:
        print("训练被手动中断...")
    finally:
        env._close()
        torch.save(agent.policy.state_dict(), os.path.join(models_dir, "last_model.pth"))
        matplotlib.rcParams['font.sans-serif'] = ['SimHei']
        matplotlib.rcParams['axes.unicode_minus'] = False
        plt.figure(figsize=(15, 8))

        plt.subplot(2, 2, 1)
        plt.plot(all_rewards)
        plt.title("回合奖励")

        plt.subplot(2, 2, 2)
        plt.plot(lane_change_counts)
        plt.title("变道次数")

        plt.subplot(2, 2, 3)
        plt.plot(collision_counts)
        plt.title("碰撞次数")

        plt.subplot(2, 2, 4)
        plt.plot(agent.total_losses)
        plt.title("训练损失")

        plt.tight_layout()
        plt.savefig(os.path.join(results_dir, "training_curves.png"))
        plt.close()

        np.savez(os.path.join(results_dir, "training_data.npz"),
                 rewards=all_rewards,
                 lane_changes=lane_change_counts,
                 collisions=collision_counts,
                 steps=total_steps_per_episode,
                 actor_losses=agent.actor_losses,
                 critic_losses=agent.critic_losses,
                 total_losses=agent.total_losses)

        print(f"训练完成，结果保存在目录: {results_dir}")

if __name__ == "__main__":
    if not (os.path.exists(Config.sumo_binary) or "SUMO_HOME" in os.environ):
        raise ValueError("SUMO路径错误，请检查SUMO是否正确安装并设置环境变量SUMO_HOME")
    if not os.path.exists(Config.config_path):
        raise ValueError(f"配置文件不存在: {Config.config_path}")
    main()
