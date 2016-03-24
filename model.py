from __future__ import division

import os
import time
import random
import numpy as np
import tensorflow as tf

from functools import partial, reduce

from backgammon.game import Game
from backgammon.player import Player
from backgammon.player_strategy import PlayerStrategy
from backgammon.strategy import random_strategy

from strategy import td_gammon_strategy

model_path = os.environ.get('MODEL_PATH', 'models/')
checkpoint_path = os.environ.get('CHECKPOINT_PATH', 'checkpoints/')
summary_path = os.environ.get('SUMMARY_PATH', 'logs/')

if not os.path.exists(model_path):
    os.makedirs(model_path)

if not os.path.exists(checkpoint_path):
    os.makedirs(checkpoint_path)

if not os.path.exists(summary_path):
    os.makedirs(summary_path)

def weight_bias(input_size, output_size):
    W = tf.Variable(tf.truncated_normal([input_size, output_size], stddev=0.1), name='weight')
    b = tf.Variable(tf.constant(0.1, shape=[output_size]), name='bias')
    return W, b

def dense_layer(x, input_size, output_size, activation, name):
    with tf.variable_scope(name):
        W, b = weight_bias(input_size, output_size)
        return activation(tf.matmul(x, W) + b, name='activation')

class Model(object):
    def __init__(self, sess, restore=False):
        # setup our session
        self.sess = sess
        self.global_step = tf.Variable(0, trainable=False, name='global_step')

        # learning rate and lambda decay
        self.alpha = tf.train.exponential_decay(0.1, self.global_step, \
            20000, 0.96, staircase=True, name='alpha') # learning rate
        self.lm = tf.train.exponential_decay(0.9, self.global_step, \
            20000, 0.96, staircase=True, name='lambda') # lambda

        alpha_summary = tf.scalar_summary(self.alpha.name, self.alpha)
        lm_summary = tf.scalar_summary(self.lm.name, self.lm)

        # setup some constants
        decay = 0.999 # ema decay rate

        # describe network size
        input_layer_size = 478
        hidden_layer_size = 40
        output_layer_size = 1

        # placeholders for input and target output
        self.x = tf.placeholder('float', [1, input_layer_size], name='x')
        self.V_next = tf.placeholder('float', [1, output_layer_size], name='V_next')

        # build network arch. (just 2 layers with sigmoid activation)
        prev_y = dense_layer(self.x, input_layer_size, hidden_layer_size, tf.sigmoid, name='layer1')
        self.V = dense_layer(prev_y, hidden_layer_size, output_layer_size, tf.sigmoid, name='layer2')

        # watch the individual value predictions over time
        tf.scalar_summary('V_next/sum', tf.reduce_sum(self.V_next))
        tf.scalar_summary('V/sum', tf.reduce_sum(self.V))

        # tf.histogram_summary(self.V_next.name, self.V_next)
        # tf.histogram_summary(self.V.name, self.V)

        # TODO: take the difference of vector containing win scenarios (incl. gammons)
        # sigma = V_next - V
        sigma_op = tf.reduce_sum(self.V_next - self.V, name='sigma')
        sigma_summary = tf.scalar_summary('sigma', sigma_op)

        sigma_ema = tf.train.ExponentialMovingAverage(decay=decay)
        sigma_ema_op = sigma_ema.apply([sigma_op])
        sigma_ema_summary = tf.scalar_summary('sigma_ema', sigma_ema.average(sigma_op))

        # mean squared error of the difference between the next state and the current state
        loss_op = tf.reduce_mean(tf.square(self.V_next - self.V), name='loss')
        loss_summary = tf.scalar_summary('loss', loss_op)

        loss_ema = tf.train.ExponentialMovingAverage(decay=decay)
        loss_ema_summary = tf.scalar_summary('loss_ema', loss_ema.average(loss_op))
        loss_ema_op = loss_ema.apply([loss_op])

        # check if the model predicts the correct winner
        accuracy_op = tf.reduce_sum(tf.cast(tf.equal(tf.round(self.V_next), tf.round(self.V)), dtype='float'), name='accuracy')
        accuracy_summary = tf.scalar_summary('accuracy', accuracy_op)

        accuracy_ema = tf.train.ExponentialMovingAverage(decay=decay)
        accuracy_ema_op = accuracy_ema.apply([accuracy_op])
        accuracy_ema_summary = tf.scalar_summary('accuracy_ema', accuracy_ema.average(accuracy_op))

        # track the number of steps and average loss for the current game
        with tf.variable_scope('game'):
            game_step = tf.Variable(tf.constant(0.0), name='game_step', trainable=False)
            game_step_op = game_step.assign_add(1.0)

            loss_sum = tf.Variable(tf.constant(0.0), name='loss_sum', trainable=False)
            loss_sum_op = loss_sum.assign_add(loss_op)
            loss_avg_op = loss_sum / tf.maximum(game_step, 1.0)
            loss_avg_summary = tf.scalar_summary('game/loss_avg', loss_avg_op)

            loss_avg_ema = tf.train.ExponentialMovingAverage(decay=decay)
            loss_avg_ema_op = loss_avg_ema.apply([loss_avg_op])
            loss_avg_ema_summary = tf.scalar_summary('game/loss_avg_ema', loss_avg_ema.average(loss_avg_op))

            # reset per-game tracking variables
            game_step_reset_op = game_step.assign(0.0)
            loss_sum_reset_op = loss_sum.assign(0.0)
            self.reset_op = tf.group(*[loss_sum_reset_op, game_step_reset_op])

        game_summaries = [
            alpha_summary,
            lm_summary,
            sigma_summary,
            loss_summary,
            accuracy_summary,
            sigma_ema_summary,
            loss_ema_summary,
            accuracy_ema_summary,
            loss_avg_summary,
            loss_avg_ema_summary
        ]

        # increment global step: we keep this as a variable so it's saved with checkpoints
        global_step_op = self.global_step.assign_add(1)

        # perform gradient updates using TD-lambda and eligibility traces

        # get gradients of output V wrt trainable variables (weights and biases)
        tvars = tf.trainable_variables()
        grads = tf.gradients(self.V, tvars) # ys wrt x in xs

        # watch the weight and gradient distributions
        for grad, tvar in zip(grads, tvars):
            tf.histogram_summary(tvar.name, tvar)
            tf.histogram_summary(tvar.name + '/gradients', grad)

        # for each variable, define operations to update the tvar with sigma,
        # taking into account the gradient as part of the eligibility trace
        grad_updates = []
        with tf.variable_scope('grad_updates'):
            for grad, tvar in zip(grads, tvars):
                with tf.variable_scope('trace'):
                    # e-> = lm * e-> + <grad of output w.r.t weights>
                    #
                    trace = tf.Variable(tf.zeros(grad.get_shape()), trainable=False, name='trace')
                    trace_op = trace.assign((self.lm * trace) + grad)
                    tf.histogram_summary(tvar.name + '/traces', trace)

                # alpha 0..1
                # sigma can be + or -
                # trace can be + or -
                final_grad = self.alpha * sigma_op * trace_op
                tf.histogram_summary(tvar.name + '/final', final_grad)

                assign_op = tvar.assign_add(final_grad)
                grad_updates.append(assign_op)

        # define single operation to apply all gradient updates
        with tf.control_dependencies([
            global_step_op,
            game_step_op,
            loss_sum_op,
            sigma_ema_op,
            loss_ema_op,
            accuracy_ema_op,
            loss_avg_ema_op
        ]):
            self.train_op = tf.group(*grad_updates, name='train')

        # merge summaries for TensorBoard
        self.game_summaries_op = tf.merge_summary(game_summaries)
        self.summaries_op = tf.merge_all_summaries()

        # create a saver for periodic checkpoints
        self.saver = tf.train.Saver(max_to_keep=1)

        # run variable initializers
        self.sess.run(tf.initialize_all_variables())

        # after training a model, we can restore checkpoints here
        if restore:
            latest_checkpoint_path = tf.train.latest_checkpoint(checkpoint_path)
            if latest_checkpoint_path:
                print('Restoring checkpoint: {0}'.format(latest_checkpoint_path))
                self.saver.restore(self.sess, latest_checkpoint_path)

    def get_output(self, game):
        return self.sess.run(self.V, feed_dict={ self.x: game.to_array() })

    def play(self):
        strategy = partial(td_gammon_strategy, self)

        white = PlayerStrategy(Player.WHITE, strategy)
        black = PlayerHuman()

        game = Game(white, black)
        game.play()

    def test(self, episodes=100):
        wins_td = 0 # TD-gammon
        wins_rand = 0 # random

        player_td = PlayerStrategy(Player.WHITE, partial(td_gammon_strategy, self))
        player_gammon = PlayerStrategy(Player.BLACK, random_strategy)

        for episode in range(episodes):
            white, black = random.sample([player_td, player_gammon], 2)
            game = Game(white, black)

            while not game.board.finished():
                game.next(draw_board=False)

            if (game.winner == Player.WHITE and game.white == player_td) \
            or (game.winner == Player.BLACK and game.black == player_td):
                wins_td += 1
            else:
                wins_rand += 1

            win_ratio = wins_td / wins_rand if wins_rand > 0 else wins_td
            print('TEST GAME [{0}] => Ratio: {1}, TD-Gammon: {2}, Random: {3}'.format(episode, win_ratio, wins_td, wins_rand))

    def train(self):
        tf.train.write_graph(self.sess.graph_def, model_path, 'td_gammon.pb', as_text=False)

        summary_writer = tf.train.SummaryWriter('{0}{1}'.format(summary_path, int(time.time()), self.sess.graph_def))
        summary_writer_game = tf.train.SummaryWriter('{0}{1}_game'.format(summary_path, int(time.time())), self.sess.graph_def)

        model_strategy = partial(td_gammon_strategy, self)
        white = PlayerStrategy(Player.WHITE, model_strategy)
        black = PlayerStrategy(Player.BLACK, model_strategy)

        test_interval = 1000
        episodes = 1000

        for episode in range(episodes):
            if episode != 0 and episode % test_interval == 0:
                self.test(episodes=100)

            game = Game(white, black)

            while not game.board.finished():
                x = game.to_array()

                game.next(draw_board=False)
                x_next = game.to_array()
                V_next = self.sess.run(self.V, feed_dict={ self.x: x_next })

                _, global_step, summaries = self.sess.run([
                    self.train_op,
                    self.global_step,
                    self.summaries_op
                ], feed_dict={ self.x: x, self.V_next: V_next })
                summary_writer.add_summary(summaries, global_step=global_step)

            x = game.to_array()
            z = game.to_win_array()

            _, global_step, summaries, summaries_game, _ = self.sess.run([
                self.train_op,
                self.global_step,
                self.summaries_op,
                self.game_summaries_op,
                self.reset_op
            ], feed_dict={ self.x: x, self.V_next: z })
            summary_writer.add_summary(summaries, global_step=global_step)
            summary_writer_game.add_summary(summaries_game, global_step=episode)

            print('TRAIN GAME [{0}]'.format(episode))
            self.saver.save(self.sess, checkpoint_path + 'checkpoint', global_step=global_step)

        summary_writer.close()
        summary_writer_game.close()
        self.test(episodes=1000)
