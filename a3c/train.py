import os
import time
import random
import logging
from collections import deque
from itertools import count

import numpy as np
import gym
import torch
import torch.nn as nn
import torch.optim as optim
import torch.nn.functional as F

from models import ActorCritic
from mario_actions import ACTIONS
from mario_wrapper import create_mario_env
from optimizers import SharedAdam
from utils import FontColor, save_checkpoint

from a3c.utils import ensure_shared_grads, choose_action
from a3c.loss import gae


LOG_FILE = 'logs/info.log'


def train(rank, args, shared_model, counter, lock, optimizer=None, device='cpu', select_sample=True):
    torch.manual_seed(args.seed + rank)

    text_color = FontColor.RED if select_sample else FontColor.GREEN
    print(text_color + f"Process: {rank: 3d} | {'Sampling' if select_sample else 'Decision'} | Device: {str(device).upper()}", FontColor.END)

    env = create_mario_env(args.env_name, ACTIONS[args.move_set])
    observation_space = env.observation_space.shape[0]
    action_space = env.action_space.n

    # env.seed(args.seed + rank)

    model = ActorCritic(observation_space, action_space)
    if torch.cuda.is_available():
        model = model.cuda()
        model.device = device

    if optimizer is None:
        optimizer = optim.Adam(shared_model.parameters(), lr=args.lr)

    model.train()

    state = env.reset()
    state = torch.from_numpy(state)
    done = True

    episode_length = 0
    for t in count():  # start=counter.value
        if t % args.save_interval == 0 and t > 0:  # and rank == 1:
            save_checkpoint(shared_model, optimizer, args, counter.value)

        model.load_state_dict(shared_model.state_dict())

        if done:
            cx = torch.zeros(1, 512)
            hx = torch.zeros(1, 512)
        else:
            cx = cx.detach()
            hx = hx.detach()

        values = []
        log_probs = []
        rewards = []
        entropies = []

        for step in range(args.num_steps):
            episode_length += 1

            value, logit, (hx, cx) = model((state.unsqueeze(0), (hx, cx)))

            prob = F.softmax(logit, dim=-1)
            log_prob = F.log_softmax(logit, dim=-1)
            entropy = -(log_prob * prob).sum(-1, keepdim=True)
            entropies.append(entropy)

            reason = ''

            if select_sample:
                action = prob.multinomial(1)
                reason = 'random'
            else:
                action = prob.max(-1, keepdim=True)[1]
                reason = 'choice'

            if torch.cuda.is_available():
                action = action.cuda()
                value = value.cuda()

            log_prob = log_prob.gather(-1, action)

            action_out = ACTIONS[args.move_set][action.item()]

            state, reward, done, info = env.step(action.item())

            info_log = {'id': args.model_id, 'rank': rank}
            info_log.update(info)
            if not os.path.exists(LOG_FILE):
                logging.basicConfig(filename=LOG_FILE, format='%(asctime)s, %(message)s', level=logging.DEBUG)
            logging.info(info_log)

            done = done or episode_length >= args.max_episode_length
            reward = max(min(reward, 50), -50)  # h/t @ArvindSoma

            with lock:
                counter.value += 1

            if done:
                episode_length = 0
                state = env.reset()

            state = torch.from_numpy(state)
            values.append(value)
            log_probs.append(log_prob)
            rewards.append(reward)

            if done:
                break

        R = torch.zeros(1, 1)
        if not done:
            value, _, _ = model((state.unsqueeze(0), (hx, cx)))
            R = value.data

        values.append(R)

        optimizer.zero_grad()
        loss = gae(R, rewards, values, log_probs, entropies, args)
        (loss).backward()
        nn.utils.clip_grad_norm_(model.parameters(), args.max_grad_norm)

        # if torch.cuda.is_available():
        #     _ = torch.cuda.synchronize()

        ensure_shared_grads(model, shared_model)

        optimizer.step()

if __name__ == "__main__":
    pass
