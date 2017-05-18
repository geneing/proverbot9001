#!/usr/bin/env python3

import subprocess
import threading
import re
import queue
import os
import os.path
import argparse
import sys
# This dependency is in pip, the python package manager
from sexpdata import *
from traceback import *

class AckError(Exception):
    def __init__(self, msg):
        self.msg = msg
    pass
class CompletedError(Exception):
    def __init__(self, msg):
        self.msg = msg
    pass

class CoqExn(Exception):
    def __init__(self, msg):
        self.msg = msg
    pass
class BadResponse(Exception):
    def __init__(self, msg):
        self.msg = msg
    pass

class SerapiInstance(threading.Thread):
    def __init__(self, coq_command, includes):
        threading.Thread.__init__(self, daemon=True)
        self._proc = subprocess.Popen(coq_command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self._fout = self._proc.stdout
        self._fin = self._proc.stdin
        self.messages = queue.Queue()
        self.start()
        self.discard_initial_feedback()
        self.exec_includes(includes)
        self.proof_context = None
        self.cur_state = 0
        self.prev_tactics = []

    def run_stmt(self, stmt):
        #print("Running statement: " + stmt)
        stmt = stmt.replace("\\", "\\\\")
        stmt = stmt.replace("\"", "\\\"")
        try:
            self._fin.write("(Control (StmAdd () \"{}\"))\n".format(stmt).encode('utf-8'))
            self._fin.flush()
            self.cur_state = self.get_next_state()

            self._fin.write("(Control (StmObserve {}))\n".format(self.cur_state).encode('utf-8'))
            self._fin.flush()
            feedbacks = self.get_feedbacks()

            self.get_proof_context()

            if self.proof_context:
                self.prev_tactics.append(stmt)
            else:
                self.prev_tactics = []

        except Exception as e:
            print("Problem running statement: {}".format(stmt))
            raise e

    def cancel_last(self):
        while not self.messages.empty():
            self.messages.get()
        cancel = "(Control (StmCancel ({})))".format(self.cur_state)
        self._fin.write(cancel.encode('utf-8'))
        self._fin.flush()
        self.get_cancelled()
        self.cur_state = self.cur_state - 1

    def get_ack(self):
        ack = self.messages.get()
        if (not isinstance(ack, list) or
            ack[0] != Symbol("Answer") or
            ack[2] != Symbol("Ack")):
            raise AckError(ack)

    def get_completed(self):
        completed = self.messages.get()
        if (completed[0] != Symbol("Answer") or
            completed[2] != Symbol("Completed")):
            raise CompletedError(completed)

    def add_lib(self, origpath, logicalpath):
        addStm = ("(Control (StmAdd () \"Add Rec LoadPath \\\"{}\\\" as {}.\"))\n"
                  .format(origpath, logicalpath))
        self._fin.write(addStm.format(origpath, logicalpath).encode('utf-8'))
        self._fin.flush()
        self.get_next_state()

    def exec_includes(self, includes_string):
        for match in re.finditer("-R\s*(\S*)\s*(\S*)\s*", includes_string):
            self.add_lib(match.group(1), match.group(2))

    def get_next_state(self):
        self.get_ack()

        msg = self.messages.get()
        while msg[0] == Symbol("Feedback"):
            msg = self.messages.get()
        if (msg[0] != Symbol("Answer")):
            raise BadResponse(msg)
        submsg = msg[2]
        if (submsg[0] == Symbol("CoqExn")):
            raise CoqExn(submsg)
        elif submsg[0] != Symbol("StmAdded"):
            raise BadResponse(submsg)
        else:
            state_num = submsg[1]
            self.get_completed()
            return state_num

    def discard_initial_feedback(self):
        feedback1 = self.messages.get()
        feedback2 = self.messages.get()
        if (feedback1[0] != Symbol("Feedback") or
            feedback2[0] != Symbol("Feedback")):
            raise BadResponse("Not feedback")

    def get_feedbacks(self):
        self.get_ack()

        feedbacks = []
        next_message = self.messages.get()
        while(next_message[0] == Symbol("Feedback")):
            feedbacks.append(next_message)
            next_message = self.messages.get()
        fin = next_message
        if (fin[0] != Symbol("Answer")):
            raise BadResponse(fin)
        if (isinstance(fin[2], list)):
            raise BadResponse(fin)
        elif(fin[2] != Symbol("Completed")):
            raise BadResponse(fin)

        return feedbacks

    def query_goals(self):
        query = "(Query () Goals)\n"
        self._fin.write(query.encode('utf-8'))
        self._fin.flush()
        self.get_ack()
        answer = self.messages.get()
        return answer
    def get_cancelled(self):
        self.get_ack()
        feedback = self.messages.get()
        cancelled = self.messages.get()
        self.get_completed()

    def extract_proof_context(self, raw_proof_context):
        return raw_proof_context[0][1]

    def get_goals(self):
        split = re.split("\n======+\n", self.proof_context)
        return split[1]

    def get_hypothesis(self):
        return re.split("\n======+\n", self.proof_context)[0]

    def get_proof_context(self):
        self._fin.write("(Query ((sid {}) (pp ((pp_format PpStr)))) Goals)".format(self.cur_state).encode('utf-8'))
        self._fin.flush()
        self.get_ack()

        proof_context_message = self.messages.get()
        if proof_context_message[0] != Symbol("Answer"):
            raise BadResponse(proof_context_message)
        else:
            ol_msg = proof_context_message[2]
            if (ol_msg[0] != Symbol("ObjList")):
                raise BadResponse(proof_context_message)
            if len(ol_msg[1]) != 0:
                self.proof_context = self.extract_proof_context(ol_msg[1])
            else:
                self.proof_context = None

    def run(self):
        while(True):
            try:
                line = self._fout.readline()
                response = loads(line.decode('utf-8'))
                # print("Got message {}".format(response))
                self.messages.put(response)
            except:
                break
    def kill(self):
        self._proc.terminate()
        self._proc.stdout.close()
        threading.Thread.join(self)

    pass