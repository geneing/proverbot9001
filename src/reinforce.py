#!/usr/bin/env python3
##########################################################################
#
#    This file is part of Proverbot9001.
#
#    Proverbot9001 is free software: you can redistribute it and/or modify
#    it under the terms of the GNU General Public License as published by
#    the Free Software Foundation, either version 3 of the License, or
#    (at your option) any later version.
#
#    Proverbot9001 is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU General Public License for more details.
#
#    You should have received a copy of the GNU General Public License
#    along with Proverbot9001.  If not, see <https://www.gnu.org/licenses/>.
#
#    Copyright 2019 Alex Sanchez-Stern and Yousef Alhessi
#
##########################################################################

import argparse
import random
import torch
import os
import errno
import signal
import traceback
import re

import serapi_instance
import dataloader
import tokenizer
from models import tactic_predictor, q_estimator
from models.features_q_estimator import FeaturesQEstimator
from models.q_estimator import QEstimator
from models import features_polyarg_predictor
import predict_tactic
import util
from util import eprint, print_time, nostderr, unwrap, progn

from dataclasses import dataclass
from typing import List, Tuple, Iterator, Optional, cast
from format import (TacticContext, ProofContext, Obligation)
from pathlib_revised import Path2

import pygraphviz as pgv
from tqdm import trange, tqdm


def main() -> None:
    util.use_cuda = False
    parser = \
        argparse.ArgumentParser(
            description="A module for exploring deep Q learning "
            "with proverbot9001")

    parser.add_argument("scrape_file")

    parser.add_argument("out_weights", type=Path2)
    parser.add_argument("environment_files", type=Path2, nargs="+")
    parser.add_argument("--proof", default=None)

    parser.add_argument("--prelude", default=".", type=Path2)

    parser.add_argument("--predictor-weights",
                        default=Path2("data/polyarg-weights.dat"),
                        type=Path2)
    parser.add_argument("--start-from", default=None, type=Path2)
    parser.add_argument("--num-predictions", default=16, type=int)

    parser.add_argument("--buffer-size", default=256, type=int)
    parser.add_argument("--batch-size", default=32, type=int)

    parser.add_argument("--num-episodes", default=256, type=int)
    parser.add_argument("--episode-length", default=16, type=int)

    parser.add_argument("--learning-rate", default=0.0001, type=float)
    parser.add_argument("--batch-step", default=50, type=int)
    parser.add_argument("--gamma", default=0.8, type=float)

    parser.add_argument("--pretrain-epochs", default=10, type=int)
    parser.add_argument("--no-pretrain", action='store_false',
                        dest='pretrain')

    parser.add_argument("--progress", "-P", action='store_true')
    parser.add_argument("--verbose", "-v", action='count', default=0)
    parser.add_argument("--log-anomalies", type=Path2, default=None)
    parser.add_argument("--log-outgoing-messages", type=Path2, default=None)

    parser.add_argument("--hardfail", action="store_true")

    parser.add_argument("--ghosts", action='store_true')
    parser.add_argument("--graphs-dir", default=Path2("graphs"), type=Path2)

    parser.add_argument("--success-repetitions", default=10, type=int)

    args = parser.parse_args()

    try:
        os.makedirs(str(args.graphs_dir))
    except OSError as e:
        if e.errno != errno.EEXIST:
            raise

    reinforce(args)


@dataclass(init=True)
class LabeledNode:
    action: str
    reward: float
    node_id: int
    parent: Optional["LabeledNode"]
    children: List["LabeledNode"]


class ReinforceGraph:
    __graph: pgv.AGraph
    __next_node_id: int
    start_node: LabeledNode

    def __init__(self, lemma_name: str) -> None:
        self.__graph = pgv.AGraph(directed=True)
        self.__next_node_id = 0
        self.start_node = self.mkNode(lemma_name, 0, None)
        pass

    def addTransition(self, src: LabeledNode, action: str, reward: float,
                      **kwargs) -> LabeledNode:
        for child in src.children:
            if child.action == action:
                assert child.reward == reward
                return child
        return self.mkNode(action, reward, src, **kwargs)

    def addGhostTransition(self, src: LabeledNode, action: str,
                           **kwargs) -> LabeledNode:
        for child in src.children:
            if child.action == action:
                return child
        return self.mkNode(action, 0, src, fillcolor="grey", **kwargs)

    def mkNode(self, action: str, reward: float,
               previous_node: Optional[LabeledNode],
               **kwargs) -> LabeledNode:
        if 'fillcolor' not in kwargs:
            if reward > 0:
                color = "palegreen"
            elif reward < 0:
                color = "indianred1"
            else:
                color = "white"
            self.__graph.add_node(self.__next_node_id, label=action,
                                  fillcolor=color, style="filled",
                                  **kwargs)
        else:
            self.__graph.add_node(self.__next_node_id, label=action)
        self.__next_node_id += 1
        newNode = LabeledNode(action, reward, self.__next_node_id-1,
                              previous_node, [])
        if previous_node:
            self.__graph.add_edge(previous_node.node_id, newNode.node_id,
                                  label=str(reward), **kwargs)
            previous_node.children.append(newNode)
        return newNode

    def mkQED(self, src: LabeledNode):
        for existing_node in src.children:
            if existing_node.action == "QED":
                return
        self.mkNode("QED", 0, src, fillcolor="green", style="filled")
        cur_node = src
        while cur_node != self.start_node:
            self.setNodeOutlineColor(cur_node, "palegreen1")
            assert cur_node.parent
            cur_node = cur_node.parent
        pass

    def setNodeColor(self, node: LabeledNode, color: str) -> None:
        node_handle = self.__graph.get_node(node.node_id)
        node_handle.attr["fillcolor"] = color
        node_handle.attr["style"] = "filled"

    def setNodeOutlineColor(self, node: LabeledNode, color: str) -> None:
        node_handle = self.__graph.get_node(node.node_id)
        node_handle.attr["color"] = color

    def setNodeApproxQScore(self, node: LabeledNode, score: float) -> None:
        node_handle = self.__graph.get_node(node.node_id)
        node_handle.attr["label"] = f"{node.action} (~{score:.2f})"

    def draw(self, filename: str) -> None:
        with nostderr():
            self.__graph.draw(filename, prog="dot")


def reinforce(args: argparse.Namespace) -> None:

    # Load the scraped (demonstrated) samples, the proof environment
    # commands, and the predictor
    replay_memory = assign_rewards(
        dataloader.tactic_transitions_from_file(args.scrape_file,
                                                args.buffer_size))

    predictor = predict_tactic.loadPredictorByFile(args.predictor_weights)

    q_estimator = FeaturesQEstimator(args.learning_rate,
                                     args.batch_step,
                                     args.gamma)
    signal.signal(
        signal.SIGINT,
        lambda signal, frame:
        progn(q_estimator.save_weights(
            args.out_weights, args),  # type: ignore
              exit()))
    if args.start_from:
        q_estimator_name, *saved = \
            torch.load(args.start_from)
        q_estimator.load_saved_state(*saved)
    elif args.pretrain:
        pre_train(args, q_estimator,
                  dataloader.tactic_transitions_from_file(
                      args.scrape_file, args.buffer_size * 10))

    epsilon = 0.3
    gamma = 0.9

    if args.proof is not None:
        assert len(args.environment_files) == 1, \
            "Can't use multiple env files with --proof!"
        env_commands = serapi_instance.load_commands_preserve(
            args, 0, args.prelude / args.environment_files[0])
        num_proofs = len([cmd for cmd in env_commands
                          if cmd.strip() == "Qed."
                          or cmd.strip() == "Defined."])

        with serapi_instance.SerapiContext(
                ["sertop", "--implicit"],
                serapi_instance.get_module_from_filename(
                    args.environment_files[0]),
                str(args.prelude)) as coq:
            coq.quiet = True
            coq.verbose = args.verbose
            rest_commands, run_commands = coq.run_into_next_proof(env_commands)
            lemma_statement = run_commands[-1]
            while coq.cur_lemma_name != args.proof:
                if not rest_commands:
                    eprint("Couldn't find lemma {args.proof}! Exiting...")
                    return
                rest_commands, _ = coq.finish_proof(rest_commands)
                rest_commands, run_commands = coq.run_into_next_proof(
                    rest_commands)
                lemma_statement = run_commands[-1]
            reinforce_lemma(args, predictor, q_estimator, coq,
                            lemma_statement,
                            epsilon, gamma, replay_memory)
            q_estimator.save_weights(args.out_weights, args)
    else:
        for env_file in args.environment_files:
            env_commands = serapi_instance.load_commands_preserve(
                args, 0, args.prelude / env_file)
            num_proofs = len([cmd for cmd in env_commands
                              if cmd.strip() == "Qed."
                              or cmd.strip() == "Defined."])
            rest_commands = env_commands
            all_run_commands: List[str] = []
            with tqdm(total=num_proofs, disable=(not args.progress),
                      leave=True, desc=env_file.stem) as pbar:
                while rest_commands:
                    with serapi_instance.SerapiContext(
                            ["sertop", "--implicit"],
                            serapi_instance.get_module_from_filename(
                                env_file),
                            str(args.prelude),
                            log_outgoing_messages=args.log_outgoing_messages) \
                            as coq:
                        coq.quiet = True
                        coq.verbose = args.verbose
                        for command in all_run_commands:
                            coq.run_stmt(command)

                        while rest_commands:
                            rest_commands, run_commands = \
                                coq.run_into_next_proof(rest_commands)
                            if not rest_commands:
                                break
                            all_run_commands += run_commands[:-1]
                            lemma_statement = run_commands[-1]

                            # Check if the definition is
                            # proof-relevant. If it is, then finishing
                            # subgoals doesn't necessarily mean you've
                            # solved the problem, so don't try to
                            # train on it.
                            proof_relevant = False
                            for cmd in rest_commands:
                                if serapi_instance.ending_proof(cmd):
                                    if cmd.strip() == "Defined.":
                                        proof_relevant = True
                                    break
                            proof_relevant = proof_relevant or \
                                bool(re.match(r"\s*Derive", lemma_statement))
                            for sample in replay_memory:
                                sample.graph_node = None
                            if not proof_relevant:
                                try:
                                    reinforce_lemma(args, predictor,
                                                    q_estimator,
                                                    coq,
                                                    lemma_statement,
                                                    epsilon, gamma,
                                                    replay_memory)
                                except serapi_instance.CoqAnomaly:
                                    if args.log_anomalies:
                                        with args.log_anomalies.open('a') as f:
                                            traceback.print_exc(file=f)
                                    if args.hardfail:
                                        eprint(
                                            "Hit an anomaly!"
                                            "Quitting due to --hardfail")
                                        raise
                                    eprint(
                                        "Hit an anomaly! "
                                        "Restarting coq instance")
                                    rest_commands.insert(0, lemma_statement)
                                    break
                            pbar.update(1)
                            rest_commands, run_commands = \
                                coq.finish_proof(rest_commands)
                            all_run_commands.append(lemma_statement)
                            all_run_commands += run_commands

            q_estimator.save_weights(args.out_weights, args)


@dataclass
class LabeledTransition:
    relevant_lemmas: List[str]
    prev_tactics: List[str]
    before: ProofContext
    after: ProofContext
    action: str
    reward: float
    graph_node: Optional[LabeledNode]

    @property
    def after_context(self) -> TacticContext:
        return TacticContext(self.relevant_lemmas,
                             self.prev_tactics,
                             self.after.focused_hyps,
                             self.after.focused_goal)

    @property
    def before_context(self) -> TacticContext:
        return TacticContext(self.relevant_lemmas,
                             self.prev_tactics,
                             self.before.focused_hyps,
                             self.before.focused_goal)


def reinforce_lemma(args: argparse.Namespace,
                    predictor: tactic_predictor.TacticPredictor,
                    estimator: q_estimator.QEstimator,
                    coq: serapi_instance.SerapiInstance,
                    lemma_statement: str,
                    epsilon: float,
                    gamma: float,
                    memory: List[LabeledTransition]) -> None:
    lemma_name = coq.cur_lemma_name
    graph = ReinforceGraph(lemma_name)
    for episode in trange(args.num_episodes, disable=(not args.progress),
                          leave=False):
        cur_node = graph.start_node
        proof_contexts_seen = [unwrap(coq.proof_context)]
        episode_memory = []
        for t in range(args.episode_length):
            with print_time("Getting predictions", guard=args.verbose):
                context_before = coq.tactic_context(coq.local_lemmas[:-1])
                proof_context_before = unwrap(coq.proof_context)
                predictions = predictor.predictKTactics(
                    context_before, args.num_predictions)
            if random.random() < epsilon:
                ordered_actions = [p.prediction for p in
                                   random.sample(predictions,
                                                 len(predictions))]
            else:
                with print_time("Picking actions using q_estimator",
                                guard=args.verbose):
                    q_choices = zip(estimator(
                        [(context_before, prediction.prediction)
                         for prediction in predictions]),
                                    [p.prediction for p in predictions])
                    ordered_actions = [p[1] for p in
                                       sorted(q_choices,
                                              key=lambda q: q[0],
                                              reverse=True)]

            with print_time("Running actions", guard=args.verbose):
                action = None
                for try_action in ordered_actions:
                    try:
                        coq.run_stmt(try_action)
                        proof_context_after = unwrap(coq.proof_context)
                        if any([serapi_instance.contextSurjective(
                                proof_context_after, path_context)
                                for path_context in proof_contexts_seen]):
                            coq.cancel_last()
                            transition = assign_failed_reward(
                                context_before.relevant_lemmas,
                                context_before.prev_tactics,
                                proof_context_before,
                                proof_context_after,
                                try_action,
                                -50)
                            assert transition.reward < 2000
                            memory.append(transition)
                            if args.ghosts:
                                ghost_node = graph.addGhostTransition(
                                    cur_node, try_action)
                                transition.graph_node = ghost_node
                            continue
                        action = try_action
                        break
                    except (serapi_instance.ParseError,
                            serapi_instance.CoqExn,
                            serapi_instance.TimeoutError):
                        transition = assign_failed_reward(
                            context_before.relevant_lemmas,
                            context_before.prev_tactics,
                            proof_context_before,
                            proof_context_before,
                            try_action,
                            -500)
                        assert transition.reward < 2000
                        memory.append(transition)
                        if args.ghosts:
                            ghost_node = graph.addGhostTransition(cur_node,
                                                                  try_action)
                            transition.graph_node = ghost_node
                        pass
                if action is None:
                    # We'll hit this case of we tried all of the
                    # predictions, and none worked
                    graph.setNodeColor(cur_node, "red")
                    break  # Break from episode

            transition = assign_reward(context_before.relevant_lemmas,
                                       context_before.prev_tactics,
                                       proof_context_before,
                                       proof_context_after,
                                       action)
            cur_node = graph.addTransition(cur_node, action,
                                           transition.reward)
            transition.graph_node = cur_node
            assert transition.reward < 2000
            episode_memory.append(transition)
            memory.append(transition)
            proof_contexts_seen.append(proof_context_after)

            if coq.goals == "":
                graph.mkQED(cur_node)
                memory += (episode_memory * (args.success_repetitions - 1))
                break

        with print_time("Assigning scores", guard=args.verbose):
            transition_samples = sample_batch(memory,
                                              args.batch_size)
            training_samples = assign_scores(transition_samples,
                                             estimator, predictor,
                                             args.num_predictions,
                                             gamma,
                                             # Passing this graph
                                             # in so we can
                                             # maintain a record
                                             # of the most recent
                                             # q score estimates
                                             # in the graph
                                             graph)
        with print_time("Training", guard=args.verbose):
            estimator.train(training_samples)

        # Clean up episode
        coq.run_stmt("Admitted.")
        coq.run_stmt(f"Reset {lemma_name}.")
        coq.run_stmt(lemma_statement)
    graphpath = (args.graphs_dir / lemma_name).with_suffix(".png")
    graph.draw(str(graphpath))
    pass


def sample_batch(transitions: List[LabeledTransition], k: int) -> \
      List[LabeledTransition]:
    return random.sample(transitions, k)


def assign_failed_reward(relevant_lemmas: List[str], prev_tactics: List[str],
                         before: ProofContext, after: ProofContext,
                         tactic: str, reward: int) \
                         -> LabeledTransition:
    return LabeledTransition(relevant_lemmas, prev_tactics, before, after,
                             tactic, reward, None)


def assign_reward(relevant_lemmas: List[str], prev_tactics: List[str],
                  before: ProofContext, after: ProofContext, tactic: str) \
      -> LabeledTransition:
    goals_changed = len(after.all_goals) - len(before.all_goals)
    if len(after.all_goals) == 0:
        reward = 1000.0
    elif goals_changed != 0:
        reward = -(goals_changed * 30.0)
        assert reward < 2000
    else:
        goal_size_reward = len(tokenizer.get_words(before.focused_goal)) - \
            len(tokenizer.get_words(after.focused_goal))
        num_hyps_reward = len(before.focused_hyps) - len(after.focused_hyps)
        reward = goal_size_reward * 3 + num_hyps_reward
        assert reward < 2000
    return LabeledTransition(relevant_lemmas, prev_tactics, before, after,
                             tactic, reward, None)


def assign_rewards(transitions: List[dataloader.ScrapedTransition]) -> \
      List[LabeledTransition]:
    def generate() -> Iterator[LabeledTransition]:
        for transition in transitions:
            yield assign_reward(transition.relevant_lemmas,
                                transition.prev_tactics,
                                context_r2py(transition.before),
                                context_r2py(transition.after),
                                transition.tactic)

    return list(generate())


def assign_scores(transitions: List[LabeledTransition],
                  q_estimator: q_estimator.QEstimator,
                  predictor: tactic_predictor.TacticPredictor,
                  num_predictions: int,
                  discount: float,
                  graph: ReinforceGraph) -> \
                  List[Tuple[TacticContext, str, float]]:
    def generate() -> Iterator[Tuple[TacticContext, str, float]]:
        prediction_lists = cast(features_polyarg_predictor
                                .FeaturesPolyargPredictor,
                                predictor) \
                                .predictKTactics_batch(
                                    [transition.after_context for
                                     transition in transitions],
                                    num_predictions)
        for transition, predictions in zip(transitions, prediction_lists):
            tactic_ctxt = transition.after_context

            if len(transition.after.all_goals) == 0:
                new_q = transition.reward
            else:
                estimates = q_estimator(
                    [(tactic_ctxt, prediction.prediction)
                     for prediction in predictions])
                estimated_future_q = \
                    discount * max(estimates)
                estimated_current_q = q_estimator([(transition.before_context,
                                                    transition.action)])[0]
                new_q = transition.reward + estimated_future_q \
                    - estimated_current_q

            assert transition.reward == transition.reward
            assert discount == discount
            assert new_q == new_q
            if transition.graph_node:
                graph.setNodeApproxQScore(transition.graph_node, new_q)
            yield TacticContext(
                transition.relevant_lemmas,
                transition.prev_tactics,
                transition.before.focused_hyps,
                transition.before.focused_goal), transition.action, new_q
    return list(generate())


def obligation_r2py(r_obl: dataloader.Obligation) -> Obligation:
    return Obligation(r_obl.hypotheses, r_obl.goal)


def context_r2py(r_context: dataloader.ProofContext) -> ProofContext:
    return ProofContext(list(map(obligation_r2py, r_context.fg_goals)),
                        list(map(obligation_r2py, r_context.bg_goals)),
                        list(map(obligation_r2py, r_context.shelved_goals)),
                        list(map(obligation_r2py, r_context.given_up_goals)))


def pre_train(args: argparse.Namespace, estimator: QEstimator,
              transitions: List[dataloader.ScrapedTransition]) -> None:
    samples = [(TacticContext(transition.relevant_lemmas,
                              transition.prev_tactics,
                              transition.before.fg_goals[0].hypotheses,
                              transition.before.fg_goals[0].goal),
                transition.tactic, 0.0) for transition in transitions
               if len(transition.before.fg_goals) > 0]
    estimator.train(samples, args.batch_size, args.pretrain_epochs)


if __name__ == "__main__":
    main()
