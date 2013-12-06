import sys
import logging
import deterministic_network
import unittest
from collections import namedtuple, defaultdict
from statemachine import sequence_generator

# Fix in final copy:
#  - include repeated classes
#  - merge ClusterMember and Member
#  - remove logging stuff


Proposal = namedtuple('Proposal', ['caller', 'cid', 'input'])
Ballot = namedtuple('Ballot', ['n', 'leader'])
ScoutId = namedtuple('ScoutId', ['address', 'ballot_num'])
CommanderId = namedtuple('CommanderId', ['address', 'slot', 'proposal'])


class defaultlist(list):

    def __getitem__(self, i):
        if i >= len(self):
            return None
        return list.__getitem__(self, i)

    def __setitem__(self, i, v):
        if i >= len(self):
            self.extend([None] * (i - len(self) + 1))
        list.__setitem__(self, i, v)


class Replica(deterministic_network.Node):

    def __init__(self):
        super(Replica, self).__init__()
        self.replica_execute_fn = None
        self.replica_state = None
        self.replica_slot_num = 0
        self.replica_proposals = defaultlist()
        self.replica_decisions = defaultlist()

    def replica_log(self, msg):
        self.logger.info("R: slot_num=%d proposals=%r decisions=%r\n| %s"
                % (self.replica_slot_num, self.replica_proposals, self.replica_decisions, msg))

    def initialize_replica(self, execute_fn, initial_value):
        self.replica_execute_fn = execute_fn
        self.state = initial_value

    def invoke(self, input):
        self.state, output = self.replica_execute_fn(self.state, input)
        return output

    def propose(self, proposal):
        slot = max(len(self.replica_proposals),
                    len(self.replica_decisions))
        self.replica_log("proposing %s at slot %d" % (proposal, slot))
        self.replica_proposals[slot] = proposal
        self.send(self.cluster_members, 'PROPOSE', slot=slot, proposal=proposal)

    def do_INVOKE(self, caller, cid, input):
        proposal = Proposal(caller, cid, input)
        if proposal not in self.replica_proposals:
            self.propose(proposal)
        else:
            slot = self.replica_proposals.index(proposal)
            self.replica_log("proposal %s already proposed in slot %d" % (proposal, slot))

    def do_DECISION(self, slot, proposal):
        if self.replica_decisions[slot] is not None:
            assert self.replica_decisions[slot] == proposal
            return
        self.replica_decisions[slot] = proposal

        # execute any pending, decided proposals, eliminating duplicates
        while True:
            decided_proposal = self.replica_decisions[self.replica_slot_num]
            if not decided_proposal:
                break  # not decided yet

            # re-propose any of our proposals which have lost in their slot
            our_proposal = self.replica_proposals[self.replica_slot_num]
            if our_proposal is not None and our_proposal != decided_proposal:
                self.propose(our_proposal)

            if decided_proposal in self.replica_decisions[:self.replica_slot_num]:
                continue  # duplicate
            self.replica_log("invoking %r" % (decided_proposal,))
            output = self.invoke(decided_proposal.input)
            self.send([proposal.caller], 'INVOKED',
                    cid=decided_proposal.cid, output=output)
            self.replica_slot_num += 1


class Acceptor(deterministic_network.Node):

    def __init__(self):
        super(Acceptor, self).__init__()
        self.acceptor_ballot_num = Ballot(-1, -1)
        self.acceptor_accepted = defaultdict()  # { (b,s) : p }

    def acceptor_log(self, msg):
        self.logger.info("A: ballot_num=%r accepted=%r\n| %s"
                % (self.acceptor_ballot_num, sorted(self.acceptor_accepted.items()), msg))

    def do_PREPARE(self, scout_id, ballot_num):  # p1a
        if ballot_num > self.acceptor_ballot_num:
            self.acceptor_ballot_num = ballot_num
        self.send([scout_id.address], 'PROMISE',  # p1b
                  scout_id=scout_id,
                  acceptor=self.address,
                  ballot_num=self.acceptor_ballot_num,
                  accepted=self.acceptor_accepted)

    def do_ACCEPT(self, commander_id, ballot_num, slot, proposal):  # p2a
        if ballot_num >= self.acceptor_ballot_num:
            self.acceptor_ballot_num = ballot_num
            self.acceptor_accepted[(ballot_num,slot)] = proposal
        self.send([commander_id.address], 'ACCEPTED',  # p2b
                  commander_id=commander_id,
                  acceptor=self.address,
                  ballot_num=self.acceptor_ballot_num)


class AcceptorTests(unittest.TestCase):

    def setUp(self):
        self.acc = Acceptor()
        self.sent = []
        self.acc.send = lambda *args, **kwargs : self.sent.append((args, kwargs))

    def assertSent(self, nodes, message, **kwargs):
        self.assertEqual(self.sent.pop(0), ((nodes, message), kwargs))

    def test_prepare_no_adopt(self):
        self.acc.acceptor_ballot_num = (11, 20)
        self.acc.do_PREPARE(leader='ldr', ballot_num=(10, 20))
        self.assertSent(['ldr'], 'PROMISE',
                acceptor=self.acc.address,
                ballot_num=(11, 20),
                accepted={})
        self.assertEqual(self.acc.acceptor_ballot_num, (11, 20))

    def test_prepare_adopt(self):
        self.acc.do_PREPARE(leader='ldr', ballot_num=(10, 20))
        self.assertSent(['ldr'], 'PROMISE',
                acceptor=self.acc.address,
                ballot_num=(10, 20),
                accepted={})
        self.assertEqual(self.acc.acceptor_ballot_num, (10, 20))

    def test_accept(self):
        p = Proposal(caller='clt', cid=1234, input='data')
        c = CommanderId(address='ldr', slot=8, proposal=p)
        self.acc.do_ACCEPT(commander_id=c, ballot_num=(10, 20), slot=8, proposal=p)
        self.assertSent(['ldr'], 'ACCEPTED',
                commander_id=c,
                acceptor=self.acc.address,
                ballot_num=(10, 20))
        self.assertEqual(self.acc.acceptor_ballot_num, (10, 20))
        self.assertEqual(self.acc.acceptor_accepted, {((10, 20), 8) : p})

    def test_accept_not(self):
        p = Proposal(caller='clt', cid=1234, input='data')
        c = CommanderId(address='ldr', slot=8, proposal=p)
        self.acc.acceptor_ballot_num = (11, 20)
        self.acc.do_ACCEPT(commander_id=c, ballot_num=(10, 20), slot=8, proposal=p)
        self.assertSent(['ldr'], 'ACCEPTED',
                commander_id=c,
                acceptor=self.acc.address,
                ballot_num=(11, 20))
        self.assertEqual(self.acc.acceptor_ballot_num, (11, 20))
        self.assertEqual(self.acc.acceptor_accepted, {})


class Scout(object):

    # scouts are indexed by ballot num, but need slot/proposal to send to
    # acceptor for proper
    PREPARE_RETRANSMIT = 1

    def __init__(self, node, ballot_num):
        self.node = node
        self.scout_id = ScoutId(self.node.address, ballot_num)
        self.scout_ballot_num = ballot_num
        self.scout_pvals = defaultdict()
        self.scout_accepted = set([])
        self.scout_quorum = len(node.cluster_members) / 2 + 1
        self.retransmit_timer = None

    def scout_log(self, msg):
        self.node.logger.info("S: ballot_num=%r pvals=%r len(accepted)=%d\n| %s"
                % (self.scout_ballot_num, sorted(self.scout_pvals.items()),
                    len(self.scout_accepted), msg))

    def start(self):
        self.scout_log("starting")
        self.send_prepare()

    def send_prepare(self):
        self.node.send(self.node.cluster_members, 'PREPARE',  # p1a
                       scout_id=self.scout_id,
                       ballot_num=self.scout_ballot_num)
        self.retransmit_timer = self.node.set_timer(self.PREPARE_RETRANSMIT, self.send_prepare)

    def finished(self, adopted, ballot_num):
        self.node.cancel_timer(self.retransmit_timer)
        self.scout_log("finished - adopted" if adopted else "finished - preempted")
        self.node.scout_finished(adopted, ballot_num, self.scout_pvals)

    def do_PROMISE(self, acceptor, ballot_num, accepted):  # p1b
        if ballot_num == self.scout_ballot_num:
            self.scout_log("got matching promise; need %d" % self.scout_quorum)
            self.scout_pvals.update(accepted)
            self.scout_accepted.add(acceptor)
            if len(self.scout_accepted) >= self.scout_quorum:
                self.finished(True, ballot_num)
        else:
            # ballot_num > self.scout_ballot_num; responses to other scouts don't
            # result in a call oto this method
            self.finished(False, ballot_num)


class Commander(object):

    def __init__(self, node, ballot_num, slot, proposal):
        self.node = node
        self.commander_ballot_num = ballot_num
        self.commander_slot = slot
        self.commander_proposal = proposal
        self.commander_id = CommanderId(node.address, slot, proposal)
        self.commander_accepted = set([])
        self.commander_quorum = len(node.cluster_members) / 2 + 1

    def commander_log(self, msg):
        self.node.logger.info("C: ballot_num=%r slot=%d proposal=%r len(accepted)=%d\n| %s"
                % (self.commander_ballot_num, self.commander_slot, self.commander_proposal, len(self.commander_accepted), msg))

    def start(self):
        self.node.send(self.node.cluster_members, 'ACCEPT',  # p2a
                       commander_id=self.commander_id,
                       ballot_num=self.commander_ballot_num,
                       slot=self.commander_slot,
                       proposal=self.commander_proposal)

    def do_ACCEPTED(self, acceptor, ballot_num):  # p2b
        if ballot_num == self.commander_ballot_num:
            self.commander_accepted.add(acceptor)
            if len(self.commander_accepted) >= self.commander_quorum:
                self.node.send(self.node.cluster_members, 'DECISION',
                               slot=self.commander_slot,
                               proposal=self.commander_proposal)
                del self.node.leader_commanders[self.commander_id]
        else:
            self.node.commander_finished(self.commander_id, ballot_num)


class Leader(deterministic_network.Node):

    def __init__(self):
        super(Leader, self).__init__()
        self.leader_ballot_num = Ballot(0, self.unique_id)
        self.leader_active = False
        self.leader_proposals = defaultlist()
        self.leader_commanders = {}
        self.leader_scout = None

    def leader_log(self, msg):
        self.logger.info("L: ballot_num=%r active=%r proposals=%r commanders=%r scout=%r\n| %s"
                % (self.leader_ballot_num, self.leader_active, self.leader_proposals, self.leader_commanders, self.leader_scout, msg))

    def start(self):
        self.spawn_scout(self.leader_ballot_num)

    def spawn_scout(self, ballot_num):
        assert not self.leader_scout
        sct = self.leader_scout = Scout(self, ballot_num)
        sct.start()

    def scout_finished(self, adopted, ballot_num, pvals):
        self.leader_scout = None
        if adopted:
            # pvals is a defaultlist of (slot, proposal) by ballot num; we need the
            # highest ballot number for each slot.  TODO: this is super inefficient!
            last_by_slot = defaultlist()
            for b, s in reversed(sorted(pvals.keys())):
                p = pvals[b, s]
                if last_by_slot[s] is not None:
                    last_by_slot[s] = p
            for s, p in enumerate(last_by_slot):
                if p is not None:
                    self.leader_proposals[s] = p
            for s, p in enumerate(self.leader_proposals):
                if p is not None:
                    self.spawn_commander(ballot_num, s, p)
            self.leader_log("becoming active")
            self.leader_active = True
        else:
            self.preempted(ballot_num)

    def commander_finished(self, commander_id, ballot_num):
        del self.leader_commanders[commander_id]
        self.preempted(ballot_num)

    def preempted(self, ballot_num):
        self.leader_log("preempted by %r" % (ballot_num,))
        if ballot_num > self.leader_ballot_num:
            self.leader_log("becoming inactive")
            self.leader_active = False
            self.leader_ballot_num = Ballot(ballot_num.n + 1, self.unique_id)
            if not self.leader_scout:
                self.spawn_scout(self.leader_ballot_num)

    def spawn_commander(self, ballot_num, slot, proposal):
        cmd = Commander(self, ballot_num, slot, proposal)
        if cmd.commander_id in self.leader_commanders:
            return
        self.leader_commanders[cmd.commander_id] = cmd
        cmd.start()

    def do_PROPOSE(self, slot, proposal):
        if self.leader_proposals[slot] is None:
            self.leader_proposals[slot] = proposal
            if self.leader_active:
                self.spawn_commander(self.leader_ballot_num, slot, proposal)
            else:
                self.logger.debug("not active - not starting commander")
        else:
            self.logger.debug("slot already full")

    def do_PROMISE(self, scout_id, acceptor, ballot_num, accepted):
        sct = self.leader_scout
        if sct and scout_id == sct.scout_id:
            sct.do_PROMISE(acceptor, ballot_num, accepted)

    def do_ACCEPTED(self, commander_id, acceptor, ballot_num):
        cmd = self.leader_commanders.get(commander_id)
        if cmd:
            cmd.do_ACCEPTED(acceptor, ballot_num)



class ClusterMember(Replica, Acceptor, Leader):

    def __init__(self, cluster_members):
        # TODO: for now, this has a static list of cluster members
        super(ClusterMember, self).__init__()
        self.cluster_members = cluster_members


if __name__ == "__main__":
    logging.basicConfig(
        format="%(asctime)s %(name)s %(message)s", level=logging.DEBUG)
    deterministic_network.Node.port = int(sys.argv[1])
    cluster_members = sys.argv[2:]
    member = ClusterMember(cluster_members)
    member.initialize_replica(sequence_generator, initial_value=0)
    member.run()
