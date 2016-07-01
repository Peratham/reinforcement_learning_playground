import numpy as np
import random
import tensorflow as tf
import time
import os
import logging
import gym
from gym import envs, scoreboard
from gym.spaces import Discrete, Box
import prettytensor as pt
import tempfile
import csv
import datetime as dt
import sys

DTYPE = tf.float32
RENDER_EVERY = 100
MONITOR = False
# Sample from probability distribution.
def cat_sample(prob_nk):
    assert prob_nk.ndim == 2
    N = prob_nk.shape[0]
    csprob_nk = np.cumsum(prob_nk, axis=1)
    out = np.zeros(N, dtype='i')
    for (n, csprob_k, r) in zip(xrange(N), csprob_nk, np.random.rand(N)):
        for (k, csprob) in enumerate(csprob_k):
            if csprob > r:
                out[n] = k
                break
    return out

def discount_rewards(r, gamma):
  """ take 1D float array of rewards and compute discounted reward """
  discounted_r = np.zeros_like(r)
  running_add = 0
  for t in reversed(xrange(0, r.size)):
    #if r[t] != 0: running_add = 0 # reset the sum, since this was a game boundary (pong specific!)
    running_add = running_add * gamma + r[t]
    discounted_r[t] = running_add
  return discounted_r

# Learning agent. Incapsulates training and prediction.
class PGAgent(object):
  def __init__(self, env, win_step, H, timesteps_per_batch, learning_rate, gamma, epochs, dropout, win_reward):
    if not isinstance(env.observation_space, Box) or \
       not isinstance(env.action_space, Discrete):
        print("Incompatible spaces.")
        exit(-1)

    self.H = H
    self.timesteps_per_batch = timesteps_per_batch
    self.learning_rate = learning_rate
    self.gamma = gamma
    self.epochs = epochs
    self.dropout = dropout
    self.win_reward = win_reward
    self.win_step = win_step
    self.env = env
    self.session = tf.Session()

    # Full state used for next action prediction. Contains current
    # observation, previous observation and previous action.
    self.obs = tf.placeholder(
        DTYPE, shape=[
            None, 2 * env.observation_space.shape[0] + env.action_space.n], name="obs")
    self.prev_obs = np.zeros(env.observation_space.shape[0])
    self.prev_action = np.zeros(env.action_space.n)

    # One hot encoding of the actual action taken.
    self.action = action = tf.placeholder(DTYPE, shape=[None, env.action_space.n], name="action")
    # Advantage, obviously.
    self.advant = advant = tf.placeholder(DTYPE, shape=[None], name="advant")
    # Old policy prediction.
    self.prev_policy = action = tf.placeholder(DTYPE, shape=[None, env.action_space.n], name="prev_policy")

    self.policy_network, _ = (
        pt.wrap(self.obs)
            .fully_connected(H, activation_fn=tf.nn.tanh)
            .dropout(self.dropout)
            .softmax_classifier(env.action_space.n))
    self.returns = tf.placeholder(DTYPE, shape=[None, env.action_space.n], name="returns")

    loss = - tf.reduce_sum(tf.mul(self.action,
                                  tf.div(self.policy_network,
                                         self.prev_policy)), 1) * self.advant
    self.train = tf.train.AdamOptimizer().minimize(loss)
    self.session.run(tf.initialize_all_variables())

  def rollout(self, max_pathlength, timesteps_per_batch):
    paths = []
    timesteps_sofar = 0
    while timesteps_sofar < timesteps_per_batch:

      obs, actions, rewards, action_dists, actions_one_hot = [], [], [], [], []
      ob = self.env.reset()
      self.prev_action *= 0.0
      self.prev_obs *= 0.0
      for x in xrange(max_pathlength):
        if not RENDER_EVERY is None and 0==x % RENDER_EVERY: env.render()
        obs_new = np.expand_dims(
            np.concatenate([ob, self.prev_obs, self.prev_action], 0), 0)

        action_dist_n = self.session.run(self.policy_network, {self.obs: obs_new})

        action = int(cat_sample(action_dist_n)[0])
        self.prev_obs = ob
        self.prev_action *= 0.0
        self.prev_action[action] = 1.0

        obs.append(ob)
        actions.append(action)
        action_dists.append(action_dist_n)
        actions_one_hot.append(np.copy(self.prev_action))

        res = list(self.env.step(action))
        if not self.win_step is None and self.win_step==len(rewards):
          rewards.append(self.win_reward)
          res[2] = True
        else:
          rewards.append(res[1])
        ob = res[0]

        if res[2]:
            path = {"obs": np.concatenate(np.expand_dims(obs, 0)),
                    "action_dists": np.concatenate(action_dists),
                    "rewards": np.array(rewards),
                    "actions": np.array(actions),
                    "actions_one_hot": np.array(actions_one_hot)}
            paths.append(path)
            self.prev_action *= 0.0
            self.prev_obs *= 0.0
            timesteps_sofar += len(path["rewards"])
            break
      else:
        timesteps_sofar += max_pathlength
    return paths

  def prepare_features(self, path):
    obs = path["obs"]
    prev_obs = np.concatenate([np.zeros((1,obs.shape[1])), path["obs"][1:]], 0)
    prev_action = path['action_dists']
    return np.concatenate([obs, prev_obs, prev_action], 1)

  def predict_for_path(self, path):
    features = self.prepare_features(path)
    return self.session.run(self.policy_network, {self.obs: features})

  def learn(self):
    self.current_observation = env.reset()

    xs,hs,dlogps,drs = [],[],[],[]

    running_reward = None
    reward_sum = 0
    episode_number = 0
    current_step = 0.0
    iteration_number = 0
    discounted_eprs = []
    mean_path_lens = []
    while True:
      paths = self.rollout(max_pathlength=10000,
                           timesteps_per_batch=self.timesteps_per_batch)

      for path in paths:
        path["prev_policy"] = self.predict_for_path(path)
        path["returns"] = discount_rewards(path["rewards"], self.gamma)
        path["advant"] = path["returns"]

      features = np.concatenate([self.prepare_features(path) for path in paths])

      advant = np.concatenate([path["advant"] for path in paths])
      advant -= advant.mean()
      advant /= (advant.std() + 1e-8)

      actions = np.concatenate([path["actions_one_hot"] for path in paths])
      prev_policy = np.concatenate([path["prev_policy"] for path in paths])

      for _ in range(self.epochs):
        self.session.run(self.train, {self.obs: features,
                                      self.advant: advant,
                                      self.action: actions,
                                      self.prev_policy: prev_policy})
      iteration_number += 1

      mean_path_len = np.mean([len(path['rewards']) for path in paths])
      mean_path_lens.append(mean_path_len)
      print "Current iteration mean_path_len: %s" % mean_path_len
      if iteration_number > 100:
        paths = self.rollout(max_pathlength=10000, timesteps_per_batch=40000)
        return np.mean([len(path['rewards']) for path in paths]), np.mean(mean_path_lens)

if __name__ == '__main__':
  seed = 1
  random.seed(seed)
  np.random.seed(seed)
  tf.set_random_seed(seed)

  env = gym.make("CartPole-v0")
  if MONITOR:
    training_dir = tempfile.mkdtemp()
    env.monitor.start(training_dir)

  agent = PGAgent(env,
                  win_step=None,
                  H=87,
                  timesteps_per_batch=1793,
                  learning_rate=0.0000022134469959712048,
                  gamma=0.9709822161334865,
                  epochs=52,
                  dropout=0.7600433610799155,
                  win_reward=317.75621177926485)
  agent.learn()
  if MONITOR:
    env.monitor.close()
    gym.upload(training_dir, api_key='sk_lgS7sCv1Qxq5HFMdQXR6Sw')
