# -*- coding: utf-8 -*-
'''
stacking.py raet protocol stacking classes
'''
# pylint: skip-file
# pylint: disable=W0611

# Import python libs
import socket
import binascii
import struct

try:
    import simplejson as json
except ImportError:
    import json

# Import ioflo libs
from ioflo.base.odicting import odict
from ioflo.base import aiding

from .. import raeting
from .. import nacling
from . import packeting
from . import estating

from ioflo.base.consoling import getConsole
console = getConsole()


class Transaction(object):
    '''
    RAET protocol transaction class
    '''
    Timeout =  5.0 # default timeout

    def __init__(self, stack=None, remote=None, kind=None, timeout=None,
                 rmt=False, bcst=False, wait=False, sid=None, tid=None,
                 txData=None, txPacket=None, rxPacket=None):
        '''
        Setup Transaction instance
        timeout of 0.0 means no timeout go forever
        '''
        self.stack = stack
        self.remote = remote
        self.kind = kind or raeting.PACKET_DEFAULTS['tk']

        if timeout is None:
            timeout = self.Timeout
        self.timeout = timeout
        self.timer = aiding.StoreTimer(self.stack.store, duration=self.timeout)

        self.rmt = rmt # cf flag
        self.bcst = bcst # bf flag
        self.wait = wait # wf flag

        self.sid = sid
        self.tid = tid

        self.txData = txData or odict() # data used to prepare last txPacket
        self.txPacket = txPacket  # last tx packet needed for retries
        self.rxPacket = rxPacket  # last rx packet needed for index

    @property
    def index(self):
        '''
        Property is transaction tuple (rf, le, re, si, ti, bf,)
        '''
        le = self.stack.local.uid
        if le == 0: # bootstrapping onto channel use ha
            le = self.stack.local.ha
        re = self.remote.uid
        if re == 0: # bootstrapping onto channel use ha from zeroth remote
            re = self.stack.remotes[0].ha
        return ((self.rmt, le, re, self.sid, self.tid, self.bcst,))

    def process(self):
        '''
        Process time based handling of transaction like timeout or retries
        '''
        pass

    def receive(self, packet):
        '''
        Process received packet Subclasses should super call this
        '''
        self.rxPacket = packet

    def transmit(self, packet):
        '''
        Queue tx duple on stack transmit queue
        '''
        try:
            self.stack.tx(packet.packed, self.remote.uid)
        except raeting.StackError as ex:
            console.terse(str(ex) + '\n')
            self.stack.incStat(self.statKey())
            self.remove(remote=self.remote, index=packet.index)
            return
        self.txPacket = packet

    def add(self, remote=None, index=None):
        '''
        Add self to remote transactions
        '''
        if not index:
            index = self.index
        if not remote:
            remote = self.remote
        remote.addTransaction(index, self)

    def remove(self, remote=None, index=None):
        '''
        Remove self from stack transactions
        '''
        if not index:
            index = self.index
        if not remote:
            remote = self.remote
        if remote:
            remote.removeTransaction(index, transaction=self)

    def statKey(self):
        '''
        Return the stat name key from class name
        '''
        return ("{0}_transaction_failure".format(self.__class__.__name__.lower()))

    def nack(self, **kwa):
        '''
        Placeholder override in sub class
        nack to terminate transaction with other side of transaction
        '''
        pass

class Initiator(Transaction):
    '''
    RAET protocol initiator transaction class
    '''
    def __init__(self, **kwa):
        '''
        Setup Transaction instance
        '''
        kwa['rmt'] = False  # force rmt to False
        super(Initiator, self).__init__(**kwa)

    def process(self):
        '''
        Process time based handling of transaction like timeout or retries
        '''
        if self.timeout > 0.0 and self.timer.expired:
            self.remove()

class Correspondent(Transaction):
    '''
    RAET protocol correspondent transaction class
    '''
    Requireds = ['sid', 'tid', 'rxPacket']

    def __init__(self, **kwa):
        '''
        Setup Transaction instance
        '''
        kwa['rmt'] = True  # force rmt to True

        missing = []
        for arg in self.Requireds:
            if arg not in kwa:
                missing.append(arg)
        if missing:
            emsg = "Missing required keyword arguments: '{0}'".format(missing)
            raise TypeError(emsg)

        super(Correspondent, self).__init__(**kwa)

class Staler(Initiator):
    '''
    RAET protocol Staler initiator transaction class
    '''
    def __init__(self, **kwa):
        '''
        Setup Transaction instance
        '''
        for key in ['kind', 'sid', 'tid', 'rxPacket']:
            if key not  in kwa:
                emsg = "Missing required keyword arguments: '{0}'".format(key)
                raise TypeError(emsg)
        super(Staler, self).__init__(**kwa)

        self.prep()

    def prep(self):
        '''
        Prepare .txData for nack to stale
        '''
        self.txData.update( sh=self.stack.local.host,
                            sp=self.stack.local.port,
                            dh=self.rxPacket.data['sh'],
                            dp=self.rxPacket.data['sp'],
                            se=self.stack.local.uid,
                            de=self.rxPacket.data['se'],
                            tk=self.kind,
                            cf=self.rmt,
                            bf=self.bcst,
                            wf=self.wait,
                            si=self.sid,
                            ti=self.tid,
                            ck=raeting.coatKinds.nada,
                            fk=raeting.footKinds.nada)

    def nack(self):
        '''
        Send nack to stale packet from correspondent.
        This is used when a correspondent packet is received but no matching
        Initiator transaction is found. So create a dummy initiator and send
        a nack packet back. Do not add transaction so don't need to remove it.
        '''
        ha = (self.rxPacket.data['sh'], self.rxPacket.data['sp'])
        emsg = "Staler {0}. Stale transaction from {1} nacking...\n".format(self.stack.name, ha )
        console.terse(emsg)
        self.stack.incStat('stale_correspondent_attempt')

        if self.rxPacket.data['se'] not in self.stack.remotes:
            emsg = "Unknown correspondent estate id '{0}'\n".format(self.rxPacket.data['se'])
            console.terse(emsg)
            self.stack.incStat('unknown_correspondent_eid')
            #return #maybe we should return and not respond at all in this case

        body = odict()
        packet = packeting.TxPacket(stack=self.stack,
                                    kind=raeting.pcktKinds.nack,
                                    embody=body,
                                    data=self.txData)
        try:
            packet.pack()
        except raeting.PacketError as ex:
            console.terse(str(ex) + '\n')
            self.stack.incStat("packing_error")
            return

        self.stack.txes.append((packet.packed, ha))
        console.terse("Staler {0}. Do Nack stale correspondent {1} at {2}\n".format(
                self.stack.name, ha, self.stack.store.stamp))
        self.stack.incStat('stale_correspondent_nack')


class Stalent(Correspondent):
    '''
    RAET protocol Stalent correspondent transaction class
    '''
    Requireds = ['kind', 'sid', 'tid', 'rxPacket']

    def __init__(self, **kwa):
        '''
        Setup Transaction instance
        '''
        super(Stalent, self).__init__(**kwa)

        self.prep()

    def prep(self):
        '''
        Prepare .txData for nack to stale
        '''
        self.txData.update( sh=self.stack.local.host,
                            sp=self.stack.local.port,
                            dh=self.rxPacket.data['sh'],
                            dp=self.rxPacket.data['sp'],
                            se=self.stack.local.uid,
                            de=self.rxPacket.data['se'],
                            tk=self.kind,
                            cf=self.rmt,
                            bf=self.bcst,
                            wf=self.wait,
                            si=self.sid,
                            ti=self.tid,
                            ck=raeting.coatKinds.nada,
                            fk=raeting.footKinds.nada)

    def nack(self):
        '''
        Send nack to stale packet from initiator.
        This is used when a initiator packet is received but with a stale session id
        So create a dummy correspondent and send a nack packet back.
        Do not add transaction so don't need to remove it.
        '''
        ha = (self.rxPacket.data['sh'], self.rxPacket.data['sp'])
        emsg = "Stalent {0}. Stale transaction from '{1}' nacking ...\n".format(self.stack.name, ha )
        console.terse(emsg)
        self.stack.incStat('stale_initiator_attempt')

        if self.rxPacket.data['se'] not in self.stack.remotes:
            emsg = "Unknown initiator estate id '{0}'\n".format(self.rxPacket.data['se'])
            console.terse(emsg)
            self.stack.incStat('unknown_initiator_eid')
            #return #maybe we should return and not respond at all in this case

        body = odict()
        packet = packeting.TxPacket(stack=self.stack,
                                    kind=raeting.pcktKinds.nack,
                                    embody=body,
                                    data=self.txData)
        try:
            packet.pack()
        except raeting.PacketError as ex:
            console.terse(str(ex) + '\n')
            self.stack.incStat("packing_error")
            return

        self.stack.txes.append((packet.packed, ha))
        console.terse("Stalent {0}. Nack stale initiator from '{1}' at {2}\n".format(
                self.stack.name, ha, self.stack.store.stamp))
        self.stack.incStat('stale_initiator_nack')

class Joiner(Initiator):
    '''
    RAET protocol Joiner Initiator class Dual of Joinent
    '''
    RedoTimeoutMin = 1.0 # initial timeout
    RedoTimeoutMax = 4.0 # max timeout

    def __init__(self, redoTimeoutMin=None, redoTimeoutMax=None,
                 cascade=False, **kwa):
        '''
        Setup Transaction instance
        '''
        kwa['kind'] = raeting.trnsKinds.join
        super(Joiner, self).__init__(**kwa)

        self.cascade = cascade

        self.redoTimeoutMax = redoTimeoutMax or self.RedoTimeoutMax
        self.redoTimeoutMin = redoTimeoutMin or self.RedoTimeoutMin
        self.redoTimer = aiding.StoreTimer(self.stack.store,
                                           duration=self.redoTimeoutMin)

        self.sid = 0 #self.remote.sid always 0 for join
        self.tid = self.remote.nextTid()
        self.prep()
        # don't dump remote yet since its ephemeral until we join and get valid eid

    def transmit(self, packet):
        '''
        Augment transmit with restart of redo timer
        '''
        super(Joiner, self).transmit(packet)
        self.redoTimer.restart()

    def receive(self, packet):
        """
        Process received packet belonging to this transaction
        """
        super(Joiner, self).receive(packet) #  self.rxPacket = packet

        if packet.data['tk'] == raeting.trnsKinds.join:
            if packet.data['pk'] == raeting.pcktKinds.ack: # maybe pending
                self.pend()
            elif packet.data['pk'] == raeting.pcktKinds.response:
                self.accept()
            elif packet.data['pk'] == raeting.pcktKinds.nack: #stale
                self.refuse()
            elif packet.data['pk'] == raeting.pcktKinds.refuse: #refused
                self.refuse()
            elif packet.data['pk'] == raeting.pcktKinds.renew: #renew
                self.renew()
            elif packet.data['pk'] == raeting.pcktKinds.reject: #rejected
                self.reject()

    def process(self):
        '''
        Perform time based processing of transaction
        '''
        if self.timeout > 0.0 and self.timer.expired:
            if self.txPacket and self.txPacket.data['pk'] == raeting.pcktKinds.request:
                self.remove(index=self.txPacket.index)#index changes after accept
            else:
                self.remove(index=self.index) # in case never sent txPacket

            console.concise("Joiner {0}. Timed out with {1} at {2}\n".format(
                    self.stack.name, self.remote.name, self.stack.store.stamp))

            return

        # need keep sending join until accepted or timed out
        if self.redoTimer.expired:
            duration = min(
                         max(self.redoTimeoutMin,
                              self.redoTimer.duration * 2.0),
                         self.redoTimeoutMax)
            self.redoTimer.restart(duration=duration)
            if (self.txPacket and
                    self.txPacket.data['pk'] == raeting.pcktKinds.request):
                self.transmit(self.txPacket) #redo
                console.concise("Joiner {0}. Redo Join with {1} at {2}\n".format(
                         self.stack.name, self.remote.name, self.stack.store.stamp))
                self.stack.incStat('redo_join')

    def prep(self):
        '''
        Prepare .txData
        '''
        self.txData.update( sh=self.stack.local.host,
                            sp=self.stack.local.port,
                            dh=self.remote.host,
                            dp=self.remote.port,
                            se=self.stack.local.uid,
                            de=self.remote.uid,
                            tk=self.kind,
                            cf=self.rmt,
                            bf=self.bcst,
                            wf=self.wait,
                            si=self.sid,
                            ti=self.tid,
                            ck=raeting.coatKinds.nada,
                            fk=raeting.footKinds.nada)

    def join(self):
        '''
        Send join request
        '''
        if self.stack.local.main:
            emsg = ("Joiner {0}. Main may not initiate join\n".format(self.stack.name))
            console.terse(emsg)
            return

        joins = self.remote.joinInProcess()
        if joins:
            emsg = ("Joiner {0}. Initiator join with{1} already in process\n".format(
                                                self.stack.name,
                                                self.remote.name))
            console.concise(emsg)
            return

        yokes = self.remote.yokeInProcess()
        if yokes: # remove any correspondent yokes
            for yoke in yokes:
                if yoke.rmt: # correspondent
                    emsg = ("Joiner {0}. Removing in process "
                            "correspondent yoke with {1}\n".format(
                                        self.stack.name,
                                        self.remote.name))
                    console.concise(emsg)
                    yoke.nack(kind=raeting.pcktKinds.refuse)

        self.remote.joined = None
        self.add()
        body = odict([('name', self.stack.local.name),
                      ('verhex', self.stack.local.signer.verhex),
                      ('pubhex', self.stack.local.priver.pubhex),
                      ('role', self.stack.local.role)])
        packet = packeting.TxPacket(stack=self.stack,
                                    kind=raeting.pcktKinds.request,
                                    embody=body,
                                    data=self.txData)
        try:
            packet.pack()
        except raeting.PacketError as ex:
            console.terse(str(ex) + '\n')
            self.stack.incStat("packing_error")
            self.remove()
            return
        console.concise("Joiner {0}. Do Join with {1} at {2}\n".format(
                        self.stack.name, self.remote.name, self.stack.store.stamp))
        self.transmit(packet)

    def renew(self):
        '''
        Reset to vacuous Road data and try joining again if not main
        Otherwise act as if rejected
        '''
        if self.stack.local.main: # main never renews so just reject
            self.refuse()
            return

        if not self.stack.local.mutable: # renew not allowed on immutable road
            emsg = ("Joiner {0}. Renew from '{1}' not allowed on immutable"
                    " road\n".format(self.stack.name, self.remote.name))
            console.terse(emsg)
            self.refuse()
            return

        console.terse("Joiner {0}. Renew from {1} at {2}\n".format(
                self.stack.name, self.remote.name, self.stack.store.stamp))
        self.stack.incStat(self.statKey())
        self.remove(index=self.txPacket.index)
        if self.remote:
            # reset remote to default values and move to zero
            self.remote.replaceStaleInitiators(renew=True)
            self.remote.sid = 0
            self.remote.tid = 0
            self.remote.rsid = 0
            if self.remote.uid != 0:
                try:
                    self.stack.moveRemote(self.remote, new=0)
                except raeting.StackError as ex:
                    console.terse(str(ex) + '\n')
                    self.reject()
                    return
            self.stack.dumpRemote(self.remote)
        self.stack.local.eid = 0
        self.stack.dumpLocal()
        self.stack.join(ha=self.remote.ha, timeout=self.timeout)

    def pend(self):
        '''
        Process ack to join packet
        '''
        if not self.stack.parseInner(self.rxPacket):
            return
        pass

    def accept(self):
        '''
        Perform acceptance in response to join response packet
        '''
        if not self.stack.parseInner(self.rxPacket):
            return

        if self.stack.local.main:
            emsg = ("Joiner {0}. Invalid accept on main\n".format(self.stack.name))
            console.terse(emsg)
            self.nack(kind=raeting.pcktKinds.reject)
            return

        data = self.rxPacket.data
        body = self.rxPacket.body.data

        leid = body.get('leid')
        if not leid: # None or zero
            emsg = "Missing or invalid local estate id in accept packet\n"
            console.terse(emsg)
            self.stack.incStat('invalid_accept')
            self.remove(index=self.txPacket.index)
            return

        reid = body.get('reid')
        if not reid: # None or zero
            emsg = "Missing or invalid remote estate id in accept packet\n"
            console.terse(emsg)
            self.stack.incStat('invalid_accept')
            self.remove(index=self.txPacket.index)
            return

        name = body.get('name')
        if not name:
            emsg = "Missing remote name in accept packet\n"
            console.terse(emsg)
            self.stack.incStat('invalid_accept')
            self.remove(index=self.txPacket.index)
            return

        verhex = body.get('verhex')
        if not verhex:
            emsg = "Missing remote verifier key in accept packet\n"
            console.terse(emsg)
            self.stack.incStat('invalid_accept')
            self.remove(index=self.txPacket.index)
            return

        pubhex = body.get('pubhex')
        if not pubhex:
            emsg = "Missing remote crypt key in accept packet\n"
            console.terse(emsg)
            self.stack.incStat('invalid_accept')
            self.remove(index=self.txPacket.index)
            return

        role = body.get('role')
        if not role:
            emsg = "Missing remote role in accept packet\n"
            console.terse(emsg)
            self.stack.incStat('invalid_accept')
            self.remove(index=self.txPacket.index)
            return

        ha = (data['sh'], data['sp'])
        if (ha in self.stack.haRemotes and
                self.remote is not self.stack.haRemotes[ha]): # something is wrong
            emsg = "Joinent {0}. Invalid ha '{1}' for remote {2}\n".format(
                            self.stack.name, ha, self.remote.name)
            console.terse(emsg)
            self.nack(kind=raeting.pcktKinds.reject)
            return

        sameRoleKeys = (role == self.remote.role and
                        verhex == self.remote.verfer.keyhex and
                        pubhex == self.remote.pubber.keyhex)

        sameAll = (sameRoleKeys and
                   name == self.remote.name and
                   ha == self.remote.ha)

        # if we have to rerole then need to change status parameter to
        # role not remote also acceptance
        if self.remote.role != role:
            self.remote.role = role # change role of remote estate

        # check if remote keys are accepted here
        status = self.stack.keep.statusRemote(self.remote,
                                              verhex=verhex,
                                              pubhex=pubhex,
                                              main=self.stack.local.main,
                                              dump=True)

        if status == raeting.acceptances.rejected:
            if sameRoleKeys:
                self.stack.removeRemote(self.remote, clear=True)
                # remove also nacks so reject
            else:
                self.nack(kind=raeting.pcktKinds.reject)
            #self.remote.joined = False
            #self.stack.dumpRemote(self.remote)
            return

        vacuous = (self.remote.uid == 0)

        # otherwise status == raeting.acceptances.accepted
        # not vacuous and not sameAll and not mutable then reject
        if not (vacuous or sameAll or self.stack.local.mutable):
            emsg = ("Joiner {0}. Invalid accept nonvacuous change or imutable "
                        "'{1}'\n".format(self.stack.name,
                                         self.remote.name))
            console.terse(emsg)
            self.nack(kind=raeting.pcktKinds.reject)
            return

        #vacuous or sameAll or self.stack.local.mutable then accept
        # check unique first so do not change road unless unique
        if (reid in self.stack.remotes and
                    self.stack.remotes[reid] is not self.remote): # non unquie reid
            emsg = "Joiner {0}. Reid '{1}' unavailable for remote {2}\n".format(
                                self.stack.name, reid, self.remote.name)
            console.terse(emsg)
            self.nack(kind=raeting.pcktKinds.reject)
            return

        if (name in self.stack.nameRemotes and
                self.stack.nameRemotes[name] is not self.remote): # non unique name
            emsg = "Joiner {0}. Name '{1}' unavailable for remote {2}\n".format(
                            self.stack.name, name, self.remote.name)
            console.terse(emsg)
            self.nack(kind=raeting.pcktKinds.reject)
            return

        if (leid in self.stack.remotes): # verify leid unique
            emsg = "Joiner {0}. Leid '{1}' unavailable for remote {2}\n".format(
                                        self.stack.name, leid, self.remote.name)
            console.terse(emsg)
            self.nack(kind=raeting.pcktKinds.reject)
            return

        #self.remote.role = role
        if verhex != self.remote.verfer.keyhex:
            self.remote.verfer = nacling.Verifier(verhex) # verify key manager
        if pubhex != self.remote.pubber.keyhex:
            self.remote.pubber = nacling.Publican(pubhex) # long term crypt key manager

        if self.remote.uid != reid: #change id of remote estate
            try:
                self.stack.moveRemote(self.remote, new=reid)
            except raeting.StackError as ex:
                console.terse(str(ex) + '\n')
                self.stack.incStat(self.statKey())
                self.remove(index=self.txPacket.index)
                return

        if self.remote.name != name: # rename remote estate to new name
            try:
                self.stack.renameRemote(self.remote, new=name)
            except raeting.StackError as ex:
                console.terse(str(ex) + '\n')
                self.stack.incStat(self.statKey())
                self.remove(index=self.txPacket.index)
                return

        if self.stack.local.uid != leid:
            self.stack.local.uid = leid # change id of local estate
            self.stack.dumpLocal() # only dump if changed

        self.remote.replaceStaleInitiators(renew=(self.sid==0))
        self.remote.nextSid() # start new session
        self.remote.joined = True #accepted
        self.stack.dumpRemote(self.remote)

        self.ackAccept()

    def refuse(self):
        '''
        Process nack to join packet refused as join already in progress or some
        other problem that does not change the joined attribute
        '''
        if not self.stack.parseInner(self.rxPacket):
            return
        console.terse("Joiner {0}. Refused by {1} at {2}\n".format(
                 self.stack.name, self.remote.name, self.stack.store.stamp))
        self.stack.incStat(self.statKey())
        self.remove(index=self.txPacket.index)

    def reject(self):
        '''
        Process nack to join packet, join rejected
        '''
        if not self.stack.parseInner(self.rxPacket):
            return
        console.terse("Joiner {0}. Rejected by {1} at {2}\n".format(
                 self.stack.name, self.remote.name, self.stack.store.stamp))
        self.stack.incStat(self.statKey())
        self.remote.joined = False
        self.stack.dumpRemote(self.remote)
        self.remove(index=self.txPacket.index)

    def ackAccept(self):
        '''
        Send ack to accept response
        '''
        body = odict()
        packet = packeting.TxPacket(stack=self.stack,
                                    kind=raeting.pcktKinds.ack,
                                    embody=body,
                                    data=self.txData)
        try:
            packet.pack()
        except raeting.PacketError as ex:
            console.terse(str(ex) + '\n')
            self.stack.incStat("packing_error")
            self.remove(index=self.txPacket.index)
            return

        console.concise("Joiner {0}. Do Accept of {1} at {2}\n".format(
                self.stack.name, self.remote.name, self.stack.store.stamp))
        self.stack.incStat("join_initiate_complete")

        self.transmit(packet)
        self.remove(index=self.txPacket.index) # self.rxPacket.index

        if self.cascade:
            self.stack.allow(duid=self.remote.uid, cascade=self.cascade, timeout=self.timeout)

    def nack(self, kind=raeting.pcktKinds.nack):
        '''
        Send nack to accept response
        '''
        body = odict()
        packet = packeting.TxPacket(stack=self.stack,
                                    kind=kind,
                                    embody=body,
                                    data=self.txData)
        try:
            packet.pack()
        except raeting.PacketError as ex:
            console.terse(str(ex) + '\n')
            self.stack.incStat("packing_error")
            self.remove(index=self.txPacket.index)
            return

        if kind == raeting.pcktKinds.refuse:
            console.terse("Joiner {0}. Do Refuse of {1} at {2}\n".format(
                    self.stack.name, self.remote.name, self.stack.store.stamp))
        elif  kind == raeting.pcktKinds.reject:
            console.terse("Joiner {0}. Do Reject of {1} at {2}\n".format(
                    self.stack.name, self.remote.name, self.stack.store.stamp))
        elif kind == raeting.pcktKinds.nack:
            console.terse("Joiner {0}. Do Nack of {1} at {2}\n".format(
                self.stack.name, self.remote.name, self.stack.store.stamp))
        else:
            console.terse("Joiner {0}. Invalid nack kind of {1} nacking anyway "
                    " at {2}\n".format(self.stack.name,
                                       kind,
                                       self.stack.store.stamp))
            kind == raeting.pcktKinds.nack
        self.stack.incStat(self.statKey())
        self.transmit(packet)
        self.remove(index=self.txPacket.index)

class Joinent(Correspondent):
    '''
    RAET protocol Joinent transaction class, dual of Joiner
    '''
    RedoTimeoutMin = 0.1 # initial timeout
    RedoTimeoutMax = 2.0 # max timeout

    def __init__(self, redoTimeoutMin=None, redoTimeoutMax=None, **kwa):
        '''
        Setup Transaction instance
        '''
        kwa['kind'] = raeting.trnsKinds.join
        super(Joinent, self).__init__(**kwa)

        self.redoTimeoutMax = redoTimeoutMax or self.RedoTimeoutMax
        self.redoTimeoutMin = redoTimeoutMin or self.RedoTimeoutMin
        self.redoTimer = aiding.StoreTimer(self.stack.store, duration=0.0)

        self.prep()

    def transmit(self, packet):
        '''
        Augment transmit with restart of redo timer
        '''
        super(Joinent, self).transmit(packet)
        self.redoTimer.restart()

    def receive(self, packet):
        """
        Process received packet belonging to this transaction
        """
        super(Joinent, self).receive(packet) #  self.rxPacket = packet

        if packet.data['tk'] == raeting.trnsKinds.join:
            if packet.data['pk'] == raeting.pcktKinds.request:
                self.join()
            elif packet.data['pk'] == raeting.pcktKinds.ack: #accepted by joiner
                self.complete()
            elif packet.data['pk'] == raeting.pcktKinds.nack: #stale
                self.refuse()
            elif packet.data['pk'] == raeting.pcktKinds.refuse: #refused
                self.refuse()
            elif packet.data['pk'] == raeting.pcktKinds.reject: #rejected
                self.reject()

    def process(self):
        '''
        Perform time based processing of transaction

        '''
        if self.timeout > 0.0 and self.timer.expired:
            self.nack() # stale
            console.concise("Joinent {0}. Timed out with {1} at {2}\n".format(
                    self.stack.name, self.remote.name, self.stack.store.stamp))
            return

        # need to perform the check for accepted status and then send accept
        if self.redoTimer.expired:
            duration = min(
                         max(self.redoTimeoutMin,
                              self.redoTimer.duration * 2.0),
                         self.redoTimeoutMax)
            self.redoTimer.restart(duration=duration)

            if (self.txPacket and
                    self.txPacket.data['pk'] == raeting.pcktKinds.response): #accept packet
                self.transmit(self.txPacket) #redo
                console.concise("Joinent {0}. Redo Accept with {1} at {2}\n".format(
                    self.stack.name, self.remote.name, self.stack.store.stamp))
                self.stack.incStat('redo_accept')
            else: #check to see if status has changed to accept after other kind
                if self.remote:
                    data = self.stack.keep.loadRemote(self.remote)
                    if data:
                        status = self.stack.keep.statusRemote(self.remote,
                                                              data['verhex'],
                                                              data['pubhex'],
                                                              main=self.stack.local.main)
                        if status == raeting.acceptances.accepted:
                            self.accept()
                        elif status == raeting.acceptances.rejected:
                            "Stack {0}: Estate '{1}' eid '{2}' keys rejected\n".format(
                                    self.stack.name, self.remote.name, self.remote.uid)
                            self.remote.joined = False
                            self.stack.dumpRemote(self.remote)
                            #self.stack.removeRemote(self.remote) #reap remote
                            self.nack(kind=raeting.pcktKinds.reject)

    def prep(self):
        '''
        Prepare .txData
        '''
        #since bootstrap transaction use the reversed seid and deid from packet
        self.txData.update(sh=self.stack.local.host,
                           sp=self.stack.local.port,
                           se=self.rxPacket.data['de'],
                           de=self.rxPacket.data['se'],
                           tk=self.kind,
                           cf=self.rmt,
                           bf=self.bcst,
                           wf=self.wait,
                           si=self.sid,
                           ti=self.tid,
                           ck=raeting.coatKinds.nada,
                           fk=raeting.footKinds.nada,)

    def join(self):
        '''
        Process join packet
        Each estate must have a set of unique credentials on the road
        The credentials are.
        eid (estate id), name, ha (host address, port)
        Each of the three credentials must be separably unique on the Road, that is
        the eid must be unique, the name must be unique, the ha must be unique.

        The other credentials are the role and keys. Multiple estates may share
        the same role and associated keys. The keys are the signing key and the
        encryption key.

        Once an estate has joined the first time it will be assigned an eid.
        Changing any of the credentials after this requires that the Road be mutable.

        '''
        if not self.stack.parseInner(self.rxPacket):
            return

        if not self.stack.local.main:
            emsg = "Joinent {0}. Invalid join not main\n".format(self.stack.name)
            console.terse(emsg)
            self.nack(kind=raeting.pcktKinds.reject)
            return

        joins = self.remote.joinInProcess()
        if joins:
            for join in joins:
                emsg = "Joinent {0}. Join with {1} already in process\n".format(
                        self.stack.name, self.remote.name)
                console.concise(emsg)
                self.stack.incStat('duplicate_join_attempt')
                if join is not self:
                    self.nack(kind=raeting.pcktKinds.refuse)
            return

        yokes = self.remote.yokeInProcess()
        if yokes: # remove any initiator yokes
            for yoke in yokes:
                if not yoke.rmt:
                    emsg = ("Joinent {0}. Removing in process initiator yoke with"
                            " {1} \n".format(self.stack.name, self.remote.name))
                    console.concise(emsg)
                    yoke.nack(kind=raeting.pcktKinds.refuse)

        #Don't add transaction yet wait till later until remote is not rejected
        data = self.rxPacket.data
        body = self.rxPacket.body.data

        name = body.get('name')
        if not name:
            emsg = "Missing remote name in join packet\n"
            console.terse(emsg)
            self.stack.incStat('invalid_join')
            self.remove(index=self.rxPacket.index)
            return

        verhex = body.get('verhex')
        if not verhex:
            emsg = "Missing remote verifier key in join packet\n"
            console.terse(emsg)
            self.stack.incStat('invalid_join')
            self.remove(index=self.rxPacket.index)
            return

        pubhex = body.get('pubhex')
        if not pubhex:
            emsg = "Missing remote crypt key in join packet\n"
            console.terse(emsg)
            self.stack.incStat('invalid_join')
            self.remove(index=self.rxPacket.index)
            return

        role = body.get('role')
        if not role:
            emsg = "Missing remote role in join packet\n"
            console.terse(emsg)
            self.stack.incStat('invalid_join')
            self.remove(index=self.rxPacket.index)
            return

        #host = data['sh']
        #port = data['sp']
        # responses use received host port since index includes
        #self.txData.update( dh=host, dp=port,)
        ha = (data['sh'], data['sp'])

        reid = data['se']
        leid = data['de']

        if (self.stack.local.uid == 0):
            emsg = "Joinent {0}. Main has invalid uid of {1}\n".format(
                                self.stack.name,  self.stack.local.uid)
            console.terse(emsg)
            self.nack(kind=raeting.pcktKinds.refuse) # refuse
            return

        vacuous = (reid == 0)

        if not vacuous: # non vacuous join
            if reid not in self.stack.remotes: # ephemeral or missing
                emsg = "Joinent {0}. Received stale reid {1} for remote {2}\n".format(
                                            self.stack.name, reid, name)
                console.terse(emsg)
                self.nack(kind=raeting.pcktKinds.renew) # refuse and renew
                return
            if self.remote is not self.stack.remotes[reid]: # something is wrong
                emsg = "Joinent {0}. Mishandled join reid '{1}' for remote {2}\n".format(
                                                    self.stack.name, reid, name)
                console.terse(emsg)
                self.nack(kind=raeting.pcktKinds.reject)
                return
        else: # vacuous join
            if ha in self.stack.haRemotes: # non ephemeral ha match
                if self.remote is not self.stack.haRemotes[ha]: # something is wrong
                    emsg = "Joinent {0}. Mishandled join ha '{1}' for remote {2}\n".format(
                                self.stack.name, ha, name)
                    console.terse(emsg)
                    self.nack(kind=raeting.pcktKinds.reject)
                    return

            elif name in self.stack.nameRemotes: # non ephemeral name match
                self.remote = self.stack.nameRemotes[name] # replace

            else: # ephemeral and unique
                self.remote.name = name
                self.remote.role = role
                self.remote.verfer = nacling.Verifier(verhex) # verify key manager
                self.remote.pubber = nacling.Publican(pubhex) # long term crypt key manager


        sameRoleKeys = (role == self.remote.role and
                        verhex == self.remote.verfer.keyhex and
                        pubhex == self.remote.pubber.keyhex)

        sameAll = (sameRoleKeys and
                   name == self.remote.name and
                   ha == self.remote.ha)

        # if we have to rerole then need to change status parameter to
        # role not remote also acceptance
        if role != self.remote.role:
            self.remote.role = role

        status = self.stack.keep.statusRemote(self.remote,
                                              verhex=verhex,
                                              pubhex=pubhex,
                                              main=self.stack.local.main,
                                              dump=True)

        if status == raeting.acceptances.rejected:
            emsg = ("Joinent {0}. Keys of role='{1}' rejected for remote"
                    "  name='{2}' eid='{3}' ha='{4}'\n".format(self.stack.name,
                                                               self.remote.role,
                                                               self.remote.name,
                                                               self.remote.uid,
                                                               self.remote.ha))
            console.concise(emsg)
            #self.remote.joined = False
            #self.stack.dumpRemote(self.remote)
            if sameRoleKeys and self.remote.uid in self.stack.remotes:
                self.stack.removeRemote(self.remote, clear=Ture) #clear remote
                #removeRemote also nacks which is a reject
            else: # reject as keys rejected
                self.nack(kind=raeting.pcktKinds.reject)
            return

        #accepted or pended
        if sameAll or self.stack.local.mutable:
            if self.remote.uid not in self.stack.remotes: # ephemeral
                try:
                    self.stack.addRemote(self.remote)
                except raeting.StackError as ex:
                    console.terse(str(ex) + '\n')
                    self.stack.incStat(self.statKey())
                    #self.remove(index=self.rxPacket.index)
                    return

                emsg = ("Joinent {0}. Added new remote name='{1}' eid='{2}' "
                        "ha='{3}' role='{4}'\n".format(self.stack.name,
                                          self.remote.name,
                                          self.remote.uid,
                                          self.remote.ha,
                                          self.remote.role))
                console.concise(emsg)
                self.stack.dumpRemote(self.remote)

            elif not sameAll:
                # do both unique checks first so only change road if both unique
                if (name in self.stack.nameRemotes and
                        self.stack.nameRemotes[name] is not self.remote): # non unique name
                    emsg = "Joinent {0}.  Name '{1}' unavailable for remote {2}\n".format(
                                    self.stack.name, name, self.remote.name)
                    console.terse(emsg)
                    self.nack(kind=raeting.pcktKinds.reject)
                    return
                if (ha in self.stack.haRemotes and
                         self.stack.haRemotes[ha] is not self.remote):
                    emsg = ("Joinent {0}. Ha '{1}' unavailable for remote"
                            " {2}\n".format(self.stack.name, str(ha), name))
                    console.terse(emsg)
                    # reject as (host, port) already in use by another estate
                    # possible udp collision nack goes to wrong host
                    # but in any event the transaction will fail
                    self.nack(kind=raeting.pcktKinds.reject)
                    return

                if name != self.remote.name:
                    try:
                        self.stack.renameRemote(self.remote, new=name)
                    except raeting.StackError as ex:
                        console.terse(str(ex) + '\n')
                        self.stack.incStat(self.statKey())
                        return
                if ha != self.remote.ha:
                    try:
                        self.stack.readdressRemote(self.remote, new=ha)
                    except raeting.StackError as ex:
                        console.terse(str(ex) + '\n')
                        self.stack.incStat(self.statKey())
                        return

                if verhex != self.remote.verfer.keyhex:
                    self.remote.verfer = nacling.Verifier(verhex) # verify key manager
                if pubhex != self.remote.pubber.keyhex:
                    self.remote.pubber = nacling.Publican(pubhex) # long term crypt key manager

                self.stack.dumpRemote(self.remote)

            # add transaction
            self.add(remote=self.remote, index=self.rxPacket.index)
            self.remote.joined = None
            if status == raeting.acceptances.accepted:
                duration = min(
                                max(self.redoTimeoutMin,
                                  self.redoTimer.duration * 2.0),
                                self.redoTimeoutMax)
                self.redoTimer.restart(duration=duration)
                self.accept()
                return

            else: # status == raeting.acceptance.pending or status == None:
                self.ackJoin()
                return

        else:  # not mutable and not sameAll so reject
            emsg = ("Joinent {0}. Attempt to change immutable road "
                        "'{1}'\n".format(self.stack.name,
                                         self.remote.name))
            console.terse(emsg)
            # reject not mutable road
            self.nack(kind=raeting.pcktKinds.reject)
            return

    def ackJoin(self):
        '''
        Send ack to join request
        '''
        body = odict()
        packet = packeting.TxPacket(stack=self.stack,
                                    kind=raeting.pcktKinds.ack,
                                    embody=body,
                                    data=self.txData)
        try:
            packet.pack()
        except raeting.PacketError as ex:
            console.terse(str(ex) + '\n')
            self.stack.incStat("packing_error")
            self.remove(index=self.rxPacket.index)
            return

        console.concise("Joinent {0}. Pending Accept of {1} at {2}\n".format(
                self.stack.name, self.remote.name, self.stack.store.stamp))
        self.transmit(packet)

    def accept(self):
        '''
        Send accept response to join request
        '''
        body = odict([ ('leid', self.remote.uid),
                       ('reid', self.stack.local.uid),
                       ('name', self.stack.local.name),
                       ('verhex', self.stack.local.signer.verhex),
                       ('pubhex', self.stack.local.priver.pubhex),
                       ('role', self.stack.local.role), ])
        packet = packeting.TxPacket(stack=self.stack,
                                    kind=raeting.pcktKinds.response,
                                    embody=body,
                                    data=self.txData)
        try:
            packet.pack()
        except raeting.PacketError as ex:
            console.terse(str(ex) + '\n')
            self.stack.incStat("packing_error")
            self.remove(index=self.rxPacket.index)
            return

        console.concise("Joinent {0}. Do Accept of {1} at {2}\n".format(
                self.stack.name, self.remote.name, self.stack.store.stamp))
        self.transmit(packet)

    def nack(self, kind=raeting.pcktKinds.nack):
        '''
        Send nack to join request.
        Sometimes nack occurs without remote being added so have to nack using ha.
        '''
        if not self.remote or self.remote.uid not in self.stack.remotes:
            self.txData.update( dh=self.rxPacket.data['sh'], dp=self.rxPacket.data['sp'],)
            ha = (self.rxPacket.data['sh'], self.rxPacket.data['sp'])
        else:
            ha = self.remote.ha

        body = odict()
        packet = packeting.TxPacket(stack=self.stack,
                                    kind=kind,
                                    embody=body,
                                    data=self.txData)
        try:
            packet.pack()
        except raeting.PacketError as ex:
            console.terse(str(ex) + '\n')
            self.stack.incStat("packing_error")
            self.remove(index=self.rxPacket.index)
            return

        if kind == raeting.pcktKinds.renew:
            console.terse("Joinent {0}. Do Renew of {1} at {2}\n".format(
                    self.stack.name, ha, self.stack.store.stamp))
        elif kind == raeting.pcktKinds.refuse:
            console.terse("Joinent {0}. Do Refuse of {1} at {2}\n".format(
                    self.stack.name, ha, self.stack.store.stamp))
        elif kind == raeting.pcktKinds.reject:
            console.terse("Joinent {0}. Do Reject of {1} at {2}\n".format(
                    self.stack.name, ha, self.stack.store.stamp))
        elif kind == raeting.pcktKinds.nack:
            console.terse("Joinent {0}. Do Nack of {1} at {2}\n".format(
                    self.stack.name, ha, self.stack.store.stamp))
        else:
            console.terse("Joinent {0}. Invalid nack kind of {1} nacking anyway "
                    " at {2}\n".format(self.stack.name,
                                       kind,
                                       self.stack.store.stamp))
            kind == raeting.pcktKinds.nack

        self.stack.incStat(self.statKey())

        if ha:
            self.stack.txes.append((packet.packed, ha))
        else:
            self.transmit(packet)
        self.remove(index=self.rxPacket.index)

    def complete(self):
        '''
        process ack to accept response
        '''
        if not self.stack.parseInner(self.rxPacket):
            return

        console.terse("Joinent {0}. Done with {1} at {2}\n".format(
                self.stack.name, self.remote.name, self.stack.store.stamp))
        self.stack.incStat("join_correspond_complete")

        self.remote.removeStaleCorrespondents(renew=(self.sid==0))
        self.remote.joined = True # accepted
        self.remote.nextSid()
        self.remote.replaceStaleInitiators()
        self.stack.dumpRemote(self.remote)
        self.remove(index=self.rxPacket.index)

    def reject(self):
        '''
        Process reject nack because keys rejected
        '''
        if not self.stack.parseInner(self.rxPacket):
            return

        console.terse("Joinent {0}. Rejected by {1} at {2}\n".format(
                self.stack.name, self.remote.name, self.stack.store.stamp))
        self.stack.incStat(self.statKey())

        self.remote.joined = False
        self.stack.dumpRemote(self.remote)
        self.remove(index=self.rxPacket.index)

    def refuse(self):
        '''
        Process refuse nack because join already in progress or stale
        '''
        if not self.stack.parseInner(self.rxPacket):
            return
        console.terse("Joinent {0}. Refused by {1} at {2}\n".format(
                 self.stack.name, self.remote.name, self.stack.store.stamp))
        self.stack.incStat(self.statKey())
        self.remove(index=self.rxPacket.index)

class Yoker(Initiator):
    '''
    RAET protocol Yoker Initiator class Dual of Yokent
    This accompishes joining but initiated by main
    '''
    RedoTimeoutMin = 1.0 # initial timeout
    RedoTimeoutMax = 4.0 # max timeout

    def __init__(self, redoTimeoutMin=None, redoTimeoutMax=None,
                 cascade=False, **kwa):
        '''
        Setup Transaction instance
        '''
        kwa['kind'] = raeting.trnsKinds.yoke
        super(Yoker, self).__init__(**kwa)

        self.cascade = cascade

        self.redoTimeoutMax = redoTimeoutMax or self.RedoTimeoutMax
        self.redoTimeoutMin = redoTimeoutMin or self.RedoTimeoutMin
        self.redoTimer = aiding.StoreTimer(self.stack.store,
                                           duration=self.redoTimeoutMin)

        self.sid = 0 #self.remote.sid
        self.tid = self.remote.nextTid()
        self.prep()

    def transmit(self, packet):
        '''
        Augment transmit with restart of redo timer
        '''
        super(Yoker, self).transmit(packet)
        self.redoTimer.restart()

    def receive(self, packet):
        """
        Process received packet belonging to this transaction
        """
        super(Yoker, self).receive(packet) #  self.rxPacket = packet

        if packet.data['tk'] == raeting.trnsKinds.join:
            if packet.data['pk'] == raeting.pcktKinds.ack: # success
                self.complete()
            elif packet.data['pk'] == raeting.pcktKinds.nack: #stale
                self.refuse()
            elif packet.data['pk'] == raeting.pcktKinds.refuse: #refused
                self.refuse()
            elif packet.data['pk'] == raeting.pcktKinds.reject: #rejected
                self.reject()

    def process(self):
        '''
        Perform time based processing of transaction
        '''
        if self.timeout > 0.0 and self.timer.expired:
            self.remove()

            console.concise("Yoker {0}. Timed out with {1} at {2}\n".format(
                    self.stack.name, self.remote.name, self.stack.store.stamp))

            return

        # need keep sending join until accepted or timed out
        if self.redoTimer.expired:
            duration = min(
                         max(self.redoTimeoutMin,
                              self.redoTimer.duration * 2.0),
                         self.redoTimeoutMax)
            self.redoTimer.restart(duration=duration)
            if (self.txPacket and
                    self.txPacket.data['pk'] == raeting.pcktKinds.request):
                self.transmit(self.txPacket) #redo
                console.concise("Yoker {0}. Redo Join with {1} at {2}\n".format(
                         self.stack.name, self.remote.name, self.stack.store.stamp))
                self.stack.incStat('redo_join')

    def prep(self):
        '''
        Prepare .txData
        '''
        self.txData.update( sh=self.stack.local.host,
                            sp=self.stack.local.port,
                            dh=self.remote.host,
                            dp=self.remote.port,
                            se=self.stack.local.uid,
                            de=self.remote.uid,
                            tk=self.kind,
                            cf=self.rmt,
                            bf=self.bcst,
                            wf=self.wait,
                            si=self.sid,
                            ti=self.tid,
                            ck=raeting.coatKinds.nada,
                            fk=raeting.footKinds.nada)

    def yoke(self):
        '''
        Send yoke request

        Only send yoke if status is accepted
        '''
        if not self.stack.local.main:
            emsg = ("Yoker {0}. Non main may not initiate yoke\n".format(self.stack.name))
            console.terse(emsg)
            return

        joins = self.remote.joinInProcess()
        if joins:
            emsg = "Yoker {0}. Join with {1} already in process\n".format(
                    self.stack.name, self.remote.name)
            console.concise(emsg)
            self.stack.incStat('unecessary_yoke_attempt')
            return

        yokes = self.remote.yokeInProcess()
        if yokes:
            emsg = "Yoker {0}. Yoke with {1} already in process\n".format(
                    self.stack.name, self.remote.name)
            self.stack.incStat('duplicate_yoke_attempt')
            console.concise(emsg)
            return

        if (self.stack.local.uid == 0):
            emsg = "Yoker {0}. Main has invalid uid of {1}\n".format(
                                self.stack.name,  self.stack.local.uid)
            console.terse(emsg)
            return

        status = self.stack.keep.statusRemote(self.remote,
                                              verhex=self.remote.verfer.keyhex,
                                              pubhex=self.remote.pubber.keyhex,
                                              main=self.stack.local.main,
                                              dump=True)

        if status == raeting.acceptances.rejected:
            emsg = ("Yoker {0}. Keys of role='{1}' rejected for remote"
                    "  name='{2}' eid='{3}' ha='{4}'\n".format(self.stack.name,
                                                               self.remote.role,
                                                               self.remote.name,
                                                               self.remote.uid,
                                                               self.remote.ha))
            console.concise(emsg)
            self.remote.joined = False
            self.stack.dumpRemote(self.remote)
            return

        if status == raeting.acceptances.pending:
            emsg = ("Yoker {0}. Keys of role='{1}' pending for remote"
                    "  name='{2}' eid='{3}' ha='{4}'\n".format(self.stack.name,
                                                               self.remote.role,
                                                               self.remote.name,
                                                               self.remote.uid,
                                                               self.remote.ha))
            console.concise(emsg)
            self.remote.joined = None
            self.stack.dumpRemote(self.remote)
            return

        self.remote.joined = None
        self.add()
        body = odict([
                      ('leid', self.remote.uid), # viewpoint of receipient
                      ('lname', self.remote.name),
                      ('lrole', self.remote.role),
                      ('lverhex', self.remote.verfer.keyhex),
                      ('lpubhex', self.remote.pubber.keyhex),
                      ('name', self.stack.local.name),
                      ('role', self.stack.local.role),
                      ('verhex', self.stack.local.signer.verhex),
                      ('pubhex', self.stack.local.priver.pubhex),
                    ])
        packet = packeting.TxPacket(stack=self.stack,
                                    kind=raeting.pcktKinds.request,
                                    embody=body,
                                    data=self.txData)
        try:
            packet.pack()
        except raeting.PacketError as ex:
            console.terse(str(ex) + '\n')
            self.stack.incStat("packing_error")
            self.remove()
            return
        console.concise("Yoker {0}. Do Yoke with {1} at {2}\n".format(
                        self.stack.name, self.remote.name, self.stack.store.stamp))
        self.transmit(packet)

    def refuse(self):
        '''
        Process nack to join packet refused as join already in progress or some
        other problem that does not change the joined attribute
        '''
        if not self.stack.parseInner(self.rxPacket):
            return
        console.terse("Yoker {0}. Refused by {1} at {2}\n".format(
                 self.stack.name, self.remote.name, self.stack.store.stamp))
        self.stack.incStat(self.statKey())
        self.remove()

    def reject(self):
        '''
        Process nack to yoke packet, join rejected
        '''
        if not self.stack.parseInner(self.rxPacket):
            return
        console.terse("Yoker {0}. Rejected by {1} at {2}\n".format(
                 self.stack.name, self.remote.name, self.stack.store.stamp))
        self.stack.incStat(self.statKey())
        self.remote.joined = False
        self.stack.dumpRemote(self.remote)
        self.remove()

    def nack(self, kind=raeting.pcktKinds.nack):
        '''
        Send nack to accept response
        '''
        body = odict()
        packet = packeting.TxPacket(stack=self.stack,
                                    kind=kind,
                                    embody=body,
                                    data=self.txData)
        try:
            packet.pack()
        except raeting.PacketError as ex:
            console.terse(str(ex) + '\n')
            self.stack.incStat("packing_error")
            self.remove()
            return

        if kind==raeting.pcktKinds.refuse:
            console.terse("Yoker {0}. Do Refuse of {1} at {2}\n".format(
                    self.stack.name, self.remote.name, self.stack.store.stamp))
        elif  kind==raeting.pcktKinds.reject:
            console.terse("Yoker {0}. Do Reject of {1} at {2}\n".format(
                    self.stack.name, self.remote.name, self.stack.store.stamp))
        elif kind == raeting.pcktKinds.nack:
            console.terse("Yoker {0}. Do Nack of {1} at {2}\n".format(
                self.stack.name, self.remote.name, self.stack.store.stamp))
        else:
            console.terse("Yoker {0}. Invalid nack kind of {1} nacking anyway "
                    " at {2}\n".format(self.stack.name,
                                       kind,
                                       self.stack.store.stamp))
            kind == raeting.pcktKinds.nack

        self.stack.incStat(self.statKey())
        self.transmit(packet)
        self.remove()

    def complete(self):
        '''
        Completion in response to yoke ack packet
        '''
        if not self.stack.parseInner(self.rxPacket):
            return

        console.concise("Joiner {0}. Done yoke with {1} at {2}\n".format(
                self.stack.name, self.remote.name, self.stack.store.stamp))
        self.stack.incStat("yoke_initiate_complete")

        self.remote.replaceStaleInitiators(renew=(self.sid==0))
        self.remote.nextSid() # start new session
        self.remote.joined = True #accepted
        self.stack.dumpRemote(self.remote)

class Yokent(Correspondent):
    '''
    RAET protocol Yokent transaction class, dual of Yoker
    '''
    RedoTimeoutMin = 0.1 # initial timeout
    RedoTimeoutMax = 2.0 # max timeout

    def __init__(self, redoTimeoutMin=None, redoTimeoutMax=None, **kwa):
        '''
        Setup Transaction instance
        '''
        kwa['kind'] = raeting.trnsKinds.yoke
        super(Yokent, self).__init__(**kwa)

        self.redoTimeoutMax = redoTimeoutMax or self.RedoTimeoutMax
        self.redoTimeoutMin = redoTimeoutMin or self.RedoTimeoutMin
        self.redoTimer = aiding.StoreTimer(self.stack.store, duration=0.0)

        self.prep()

    def transmit(self, packet):
        '''
        Augment transmit with restart of redo timer
        '''
        super(Yokent, self).transmit(packet)
        self.redoTimer.restart()

    def receive(self, packet):
        """
        Process received packet belonging to this transaction
        """
        super(Yokent, self).receive(packet) #  self.rxPacket = packet

        if packet.data['tk'] == raeting.trnsKinds.yoke:
            if packet.data['pk'] == raeting.pcktKinds.request:
                self.yoke()
            elif packet.data['pk'] == raeting.pcktKinds.nack: #stale
                self.refuse()
            elif packet.data['pk'] == raeting.pcktKinds.refuse: #refused
                self.refuse()
            elif packet.data['pk'] == raeting.pcktKinds.reject: #rejected
                self.reject()

    def process(self):
        '''
        Perform time based processing of transaction

        '''
        if self.timeout > 0.0 and self.timer.expired:
            self.nack() # stale
            console.concise("Yokent {0}. Timed out with {1} at {2}\n".format(
                    self.stack.name, self.remote.name, self.stack.store.stamp))
            return

    def prep(self):
        '''
        Prepare .txData
        '''
        #since bootstrap transaction use the reversed seid and deid from packet
        self.txData.update(sh=self.stack.local.host,
                           sp=self.stack.local.port,
                           se=self.rxPacket.data['de'],
                           de=self.rxPacket.data['se'],
                           tk=self.kind,
                           cf=self.rmt,
                           bf=self.bcst,
                           wf=self.wait,
                           si=self.sid,
                           ti=self.tid,
                           ck=raeting.coatKinds.nada,
                           fk=raeting.footKinds.nada,)

    def yoke(self):
        '''
        Handle yoke request packet
        '''
        if not self.stack.parseInner(self.rxPacket):
            return

        if self.stack.local.main:
            emsg = "Yokent {0}. Invalid yoke on main\n".format(self.stack.name)
            console.terse(emsg)
            self.nack(kind=raeting.pcktKinds.reject)
            return

        joins = self.remote.joinInProcess()
        if joins:
            emsg = "Yokent {0}. Join with {1} already in process\n".format(
                    self.stack.name, self.remote.name)
            console.concise(emsg)
            self.stack.incStat('unnecessary_yoke_attempt')
            self.nack(kind=raeting.pcktKinds.refuse)
            return

        yokes = self.remote.yokeInProcess()
        if yokes:
            for yoke in yokes:
                emsg = "Yokent {0}. Yoke with {1} already in process\n".format(
                        self.stack.name, self.remote.name)
                console.concise(emsg)
                self.stack.incStat('duplicate_yoke_attempt')
                if yoke is not self:
                    self.nack(kind=raeting.pcktKinds.refuse)
            return

        data = self.rxPacket.data
        body = self.rxPacket.body.data

        leid = body.get('leid')
        if not leid: # None or zero
            emsg = "Missing or invalid local estate id in yoke packet\n"
            console.terse(emsg)
            self.stack.incStat('invalid_yoke')
            self.remove(index=self.rxPacket.index)
            return

        lname = body.get('lname')
        if not lname:
            emsg = "Missing or invalid local estate name in yoke packet\n"
            console.terse(emsg)
            self.stack.incStat('invalid_yoke')
            self.remove(index=self.rxPacket.index)
            return

        lrole = body.get('lrole')
        if not lrole:
            emsg = "Missing or invalid local estate role in yoke packet\n"
            console.terse(emsg)
            self.stack.incStat('invalid_yoke')
            self.remove(index=self.rxPacket.index)
            return

        lverhex = body.get('lverhex')
        if not lverhex:
            emsg = "Missing or invalid local estate verhex in yoke packet\n"
            console.terse(emsg)
            self.stack.incStat('invalid_yoke')
            self.remove(index=self.rxPacket.index)
            return

        lpubhex = body.get('lpubhex')
        if not lpubhex:
            emsg = "Missing or invalid local estate pubhex in yoke packet\n"
            console.terse(emsg)
            self.stack.incStat('invalid_yoke')
            self.remove(index=self.rxPacket.index)
            return

        name = body.get('name')
        if not name:
            emsg = "Missing remote name in yoke packet\n"
            console.terse(emsg)
            self.stack.incStat('invalid_yoke')
            self.remove(index=self.rxPacket.index)
            return

        role = body.get('role')
        if not role:
            emsg = "Missing remote role in yoke packet\n"
            console.terse(emsg)
            self.stack.incStat('invalid_yoke')
            self.remove(index=self.rxPacket.index)
            return

        verhex = body.get('verhex')
        if not verhex:
            emsg = "Missing remote verifier key in yoke packet\n"
            console.terse(emsg)
            self.stack.incStat('invalid_yoke')
            self.remove(index=self.rxPacket.index)
            return

        pubhex = body.get('pubhex')
        if not pubhex:
            emsg = "Missing remote crypt key in yoke packet\n"
            console.terse(emsg)
            self.stack.incStat('invalid_yoke')
            self.remove(index=self.rxPacket.index)
            return

        reid = data['se']
        if not reid: # reid of zero not allowed
            emsg = "Invalid source remote estate id in yoke packet header\n"
            console.terse(emsg)
            self.stack.incStat('invalid_yoke')
            self.remove(index=self.rxPacket.index)
            return

        ha = (data['sh'], data['sp'])

        localSameAll = (lrole == self.stack.local.role and
                        lverhex == self.stack.local.signer.verhex and
                        lpubhex == self.stack.local.priver.keyhex and
                        lname == self.stack.local.name and
                        leid == self.stack.local.uid )

        # what if data['de'] == 0 and self.stack.local.uid == 0

        vacuous = (self.remote.uid == 0)

        if not vacuous: # non vacuous join
            if self.remote.uid not in self.stack.remotes: # ephemeral
                emsg = ("Yokent {0}. Ephemeral non vacuous yoke from remote {1} "
                        "ha={2} \n".format(self.stack.name, name, ha))
                console.terse(emsg)
                self.renew()
                return

            if reid not in self.stack.remotes: # not ephemeral ha matched
                if self.remote is not self.stack.haRemotes[ha]: # something is wrong
                    emsg = "Yokent {0}. Mishandled yoke ha '{1}' for remote {2}\n".format(
                                self.stack.name, ha, name)
                    console.terse(emsg)
                    self.nack(kind=raeting.pcktKinds.reject)
                    return

                # not ephemeral matched ha
                emsg = ("Yokent {0}. Inconsistent yoke from remote {1} "
                        "ha={2} uid={3}\n".format(self.stack.name,
                                                 self.remote.name,
                                                 self.remote.ha,
                                                 self.remote.uid))
                console.terse(emsg)
                if self.local.mutable:
                    self.renew()
                else:
                    self.nack(kind=raeting.pcktKinds.reject)
                return

            # not ephemeral reid match verify consistent
            if self.remote is not self.stack.remotes[reid]: # something is wrong
                emsg = "Yokent {0}. Mishandled yoke reid '{1}' for remote {2}\n".format(
                                                    self.stack.name, reid, name)
                console.terse(emsg)
                self.nack(kind=raeting.pcktKinds.reject)
                return

            if not localSameAll:
                emsg = ("Yokent {0}. Local yoke credentials do not match from remote {2}"
                        " leid='{1}' role='{2}' verhex='{3}' pubhex='{4}'\n".format(
                                self.stack.name,
                                self.remote.name,
                                leid,
                                lname,
                                lrole,
                                lverhex,
                                lpubhex))
                console.terse(emsg)

                if self.local.mutable:
                    self.renew()
                else:
                    self.nack(kind=raeting.pcktKinds.reject)
                return

        else: # vacuous join
            if ha not in self.stack.remotes: # ephemeral
                emsg = "Yokent {0}. Vacuous and ephemeral for remote {2}\n".format(
                                                        self.stack.name, name)
                console.terse(emsg)
                if name in self.nameRemotes:
                    if self.remote is not self.stack.nameRemotes[name]: # name collision
                        pass
                        # what to do there is another remote at a same name and but
                        # different ha so not unique
                self.remote.name = name
                self.remote.role = role
                self.remote.verfer = nacling.Verifier(verhex) # verify key manager
                self.remote.pubber = nacling.Publican(pubhex) # long term crypt key manager
                self.renew()
                return

            else: # non ephemeral ha match
                if self.remote is not self.stack.haRemotes[ha]: # something is wrong
                    emsg = "Yokent {0}. Mishandled vacuous yoke ha '{1}' for remote {2}\n".format(
                                self.stack.name, ha, name)
                    console.terse(emsg)
                    self.nack(kind=raeting.pcktKinds.reject)
                    return

            if not localSameAll:
                emsg = ("Yokent {0}. Local yoke credentials do not match from remote {2}"
                        " leid='{1}' role='{2}' verhex='{3}' pubhex='{4}'\n".format(
                                self.stack.name,
                                self.remote.name,
                                leid,
                                lname,
                                lrole,
                                lverhex,
                                lpubhex))
                console.terse(emsg)
                self.nack(kind=raeting.pcktKinds.reject)
                return


        sameRoleKeys = (role == self.remote.role and
                        verhex == self.remote.verfer.keyhex and
                        pubhex == self.remote.pubber.keyhex)

        sameAll = (sameRoleKeys and
                   name == self.remote.name and
                   ha == self.remote.ha)

        # if we have to rerole then need to change status parameter to
        # role not remote also acceptance
        if self.remote.role != role:
            self.remote.role = role # change role of remote estate

        # check if remote keys are accepted here
        status = self.stack.keep.statusRemote(self.remote,
                                              verhex=verhex,
                                              pubhex=pubhex,
                                              main=self.stack.local.main,
                                              dump=True)

        if status == raeting.acceptances.rejected:
            if sameRoleKeys:
                self.stack.removeRemote(self.remote, clear=True)
                # remove also nacks so reject
            else:
                self.nack(kind=raeting.pcktKinds.reject)
            #self.remote.joined = False
            #self.stack.dumpRemote(self.remote)
            return

        # otherwise status == raeting.acceptances.accepted
        # not vacuous and not sameAll and not mutable then reject
        if not (self.remote.uid == 0 or sameAll or self.stack.local.mutable):
            emsg = ("Yokent {0}. Invalid yoke nonvacuous change or imutable "
                        "'{1}'\n".format(self.stack.name,
                                         self.remote.name))
            console.terse(emsg)
            self.nack(kind=raeting.pcktKinds.reject)
            return

        #self.remote.reid == 0 or sameAll or self.stack.local.mutable then accept
        # check unique first so do not change road unless unique
        if (reid in self.stack.remotes and
                    self.stack.remotes[reid] is not self.remote): # non unquie reid
            emsg = "Yokent {0}. Reid '{1}' unavailable for remote {2}\n".format(
                                self.stack.name, reid, self.remote.name)
            console.terse(emsg)
            self.nack(kind=raeting.pcktKinds.reject)
            return

        if (name in self.stack.nameRemotes and
                self.stack.nameRemotes[name] is not self.remote): # non unique name
            emsg = "Yokent {0}. Name '{1}' unavailable for remote {2}\n".format(
                            self.stack.name, name, self.remote.name)
            console.terse(emsg)
            self.nack(kind=raeting.pcktKinds.reject)
            return

        if (leid in self.stack.remotes): # verify leid unique
            emsg = "Yokent {0}. Leid '{1}' unavailable for remote {2}\n".format(
                                        self.stack.name, leid, self.remote.name)
            console.terse(emsg)
            self.nack(kind=raeting.pcktKinds.reject)
            return

        #self.remote.role = role
        if verhex != self.remote.verfer.keyhex:
            self.remote.verfer = nacling.Verifier(verhex) # verify key manager
        if pubhex != self.remote.pubber.keyhex:
            self.remote.pubber = nacling.Publican(pubhex) # long term crypt key manager

        if self.remote.uid != reid: #change id of remote estate
            try:
                self.stack.moveRemote(self.remote, new=reid)
            except raeting.StackError as ex:
                console.terse(str(ex) + '\n')
                self.stack.incStat(self.statKey())
                self.nack(kind=raeting.pcktKinds.refuse)
                return

        if self.remote.name != name: # rename remote estate to new name
            try:
                self.stack.renameRemote(self.remote, new=name)
            except raeting.StackError as ex:
                console.terse(str(ex) + '\n')
                self.stack.incStat(self.statKey())
                self.nack(kind=raeting.pcktKinds.refuse)
                return

        if self.stack.local.uid != leid:
            self.stack.local.uid = leid # change id of local estate
            self.stack.dumpLocal() # only dump if changed

        console.terse("Yokent {0}. Done with {1} at {2}\n".format(
                self.stack.name, self.remote.name, self.stack.store.stamp))
        self.stack.incStat("yoke_correspond_complete")

        # add transaction
        self.add(remote=self.remote, index=self.rxPacket.index)
        self.remote.removeStaleCorrespondents(renew=(self.sid==0))
        self.remote.joined = True # accepted
        self.remote.nextSid()
        self.remote.replaceStaleInitiators()
        self.stack.dumpRemote(self.remote)
        self.remove(index=self.rxPacket.index)

        self.ackYoke()

    def ackYoke(self):
        '''
        Send ack to yoke request
        '''
        body = odict()
        packet = packeting.TxPacket(stack=self.stack,
                                    kind=raeting.pcktKinds.ack,
                                    embody=body,
                                    data=self.txData)
        try:
            packet.pack()
        except raeting.PacketError as ex:
            console.terse(str(ex) + '\n')
            self.stack.incStat("packing_error")
            self.remove(index=self.rxPacket.index)
            return

        console.concise("Yokent {0}. Do ack of {1} at {2}\n".format(
                self.stack.name, self.remote.name, self.stack.store.stamp))
        self.stack.incStat("yoke_correspond_complete")

        self.transmit(packet)
        self.remove(index=self.rxPacket.index) # self.rxPacket.index

        if self.cascade:
            self.stack.allow(duid=self.remote.uid, cascade=self.cascade, timeout=self.timeout)

    def renew(self):
        '''
        Reset to vacuous Road data and try joining again
        '''
        console.terse("Yokent {0}. Renew with {1} at {2}\n".format(
                self.stack.name, self.remote.name, self.stack.store.stamp))
        self.stack.incStat(self.statKey())
        self.nack(kind=raeting.pcktKinds.refuse)
        self.remove(index=self.rxPacket.index)
        if self.remote:
            # reset remote to default values and move to zero
            self.remote.replaceStaleInitiators(renew=True)
            self.remote.sid = 0
            self.remote.tid = 0
            self.remote.rsid = 0
            if self.remote.uid != 0:
                try:
                    self.stack.moveRemote(self.remote, new=0)
                except raeting.StackError as ex:
                    console.terse(str(ex) + '\n')
                    self.remote.joined = False
                    self.stack.dumpRemote(self.remote)
                    return
            self.stack.dumpRemote(self.remote)
        self.stack.local.eid = 0
        self.stack.dumpLocal()
        self.stack.join(ha=self.remote.ha, timeout=self.timeout)

    def nack(self, kind=raeting.pcktKinds.nack):
        '''
        Send nack to join request.
        Sometimes nack occurs without remote being added so have to nack using ha.
        '''
        if not self.remote or self.remote.uid not in self.stack.remotes:
            self.txData.update( dh=self.rxPacket.data['sh'], dp=self.rxPacket.data['sp'],)
            ha = (self.rxPacket.data['sh'], self.rxPacket.data['sp'])
        else:
            ha = self.remote.ha

        body = odict()
        packet = packeting.TxPacket(stack=self.stack,
                                    kind=kind,
                                    embody=body,
                                    data=self.txData)
        try:
            packet.pack()
        except raeting.PacketError as ex:
            console.terse(str(ex) + '\n')
            self.stack.incStat("packing_error")
            self.remove(index=self.rxPacket.index)
            return

        if kind == raeting.pcktKinds.refuse:
            console.terse("Yokent {0}. Do Refuse of {1} at {2}\n".format(
                    self.stack.name, ha, self.stack.store.stamp))
        elif kind == raeting.pcktKinds.reject:
            console.terse("Yokent {0}. Do Reject of {1} at {2}\n".format(
                    self.stack.name, ha, self.stack.store.stamp))
        elif kind == raeting.pcktKinds.nack:
            console.terse("Yokent {0}. Do Nack of {1} at {2}\n".format(
                    self.stack.name, ha, self.stack.store.stamp))
        else:
            console.terse("Yokent {0}. Invalid nack kind of {1} nacking anyway "
                          " at {2}\n".format( self.stack.name,
                                              kind,
                                              self.stack.store.stamp))
            kind == raeting.pcktKinds.nack

        self.stack.incStat(self.statKey())

        if ha:
            self.stack.txes.append((packet.packed, ha))
        else:
            self.transmit(packet)
        self.remove(index=self.rxPacket.index)

    def reject(self):
        '''
        Process reject nack because keys rejected
        '''
        if not self.stack.parseInner(self.rxPacket):
            return

        console.terse("Yokent {0}. Rejected by {1} at {2}\n".format(
                self.stack.name, self.remote.name, self.stack.store.stamp))
        self.stack.incStat(self.statKey())

        self.remote.joined = False
        self.stack.dumpRemote(self.remote)
        self.remove(index=self.rxPacket.index)

    def refuse(self):
        '''
        Process refuse nack because join already in progress or stale
        '''
        if not self.stack.parseInner(self.rxPacket):
            return
        console.terse("Yokent {0}. Refused by {1} at {2}\n".format(
                 self.stack.name, self.remote.name, self.stack.store.stamp))
        self.stack.incStat(self.statKey())
        self.remove(index=self.rxPacket.index)

class Allower(Initiator):
    '''
    RAET protocol Allower Initiator class Dual of Allowent
    CurveCP handshake
    '''
    Timeout = 4.0
    RedoTimeoutMin = 0.25 # initial timeout
    RedoTimeoutMax = 1.0 # max timeout

    def __init__(self, redoTimeoutMin=None, redoTimeoutMax=None,
                 cascade=False, **kwa):
        '''
        Setup instance
        '''
        kwa['kind'] = raeting.trnsKinds.allow
        super(Allower, self).__init__(**kwa)

        self.cascade = cascade

        self.redoTimeoutMax = redoTimeoutMax or self.RedoTimeoutMax
        self.redoTimeoutMin = redoTimeoutMin or self.RedoTimeoutMin
        self.redoTimer = aiding.StoreTimer(self.stack.store,
                                           duration=self.redoTimeoutMin)

        self.sid = self.remote.sid
        self.tid = self.remote.nextTid()
        self.oreo = None # cookie from correspondent needed until handshake completed
        self.prep() # prepare .txData

    def transmit(self, packet):
        '''
        Augment transmit with restart of redo timer
        '''
        super(Allower, self).transmit(packet)
        self.redoTimer.restart()

    def receive(self, packet):
        """
        Process received packet belonging to this transaction
        """
        super(Allower, self).receive(packet) #  self.rxPacket = packet

        if packet.data['tk'] == raeting.trnsKinds.allow:
            if packet.data['pk'] == raeting.pcktKinds.cookie:
                self.cookie()
            elif packet.data['pk'] == raeting.pcktKinds.ack:
                self.allow()
            elif packet.data['pk'] == raeting.pcktKinds.nack: # rejected
                self.refuse()
            elif packet.data['pk'] == raeting.pcktKinds.refuse: # refused
                self.refuse()
            elif packet.data['pk'] == raeting.pcktKinds.reject: #rejected
                self.reject()
            elif packet.data['pk'] == raeting.pcktKinds.unjoined: # unjoined
                self.unjoin()

    def process(self):
        '''
        Perform time based processing of transaction
        '''
        if self.timeout > 0.0 and self.timer.expired:
            self.remove()
            console.concise("Allower {0}. Timed out with {1} at {2}\n".format(
                    self.stack.name, self.remote.name, self.stack.store.stamp))
            return

        # need keep sending join until accepted or timed out
        if self.redoTimer.expired:
            duration = min(
                         max(self.redoTimeoutMin,
                              self.redoTimer.duration * 2.0),
                         self.redoTimeoutMax)
            self.redoTimer.restart(duration=duration)
            if self.txPacket:
                if self.txPacket.data['pk'] == raeting.pcktKinds.hello:
                    self.transmit(self.txPacket) # redo
                    console.concise("Allower {0}. Redo Hello with {1} at {2}\n".format(
                            self.stack.name, self.remote.name, self.stack.store.stamp))
                    self.stack.incStat('redo_hello')

                if self.txPacket.data['pk'] == raeting.pcktKinds.initiate:
                    self.transmit(self.txPacket) # redo
                    console.concise("Allower {0}. Redo Initiate with {1} at {2}\n".format(
                             self.stack.name, self.remote.name, self.stack.store.stamp))
                    self.stack.incStat('redo_initiate')

                if self.txPacket.data['pk'] == raeting.pcktKinds.ack:
                    self.transmit(self.txPacket) # redo
                    console.concise("Allower {0}. Redo Ack Final with {1} at {2}\n".format(
                             self.stack.name, self.remote.name, self.stack.store.stamp))
                    self.stack.incStat('redo_final')

    def prep(self):
        '''
        Prepare .txData
        '''
        self.txData.update( sh=self.stack.local.host,
                            sp=self.stack.local.port,
                            dh=self.remote.host,
                            dp=self.remote.port,
                            se=self.stack.local.uid,
                            de=self.remote.uid, #self.reid,
                            tk=self.kind,
                            cf=self.rmt,
                            bf=self.bcst,
                            wf=self.wait,
                            si=self.sid,
                            ti=self.tid, )

    def hello(self):
        '''
        Send hello request
        '''
        allows = self.remote.allowInProcess()
        if allows:
            if self.stack.local.main:
                emsg = "Allower {0}. Allow with {1} already in process\n".format(
                        self.stack.name, self.remote.name)
                console.concise(emsg)
                return
            else: # not main so remove any correspondent allows
                already = False
                for allow in allows:
                    if allow.rmt:
                        emsg = ("Allower {0}. Removing correspondent allow with"
                                " {1} already in process\n".format(
                                            self.stack.name,
                                            self.remote.name))
                        console.concise(emsg)
                        allow.nack(kind=raeting.pcktKinds.refuse)
                    else: # already initiated
                        already = True
                if already:
                    emsg = ("Allower {0}. Initiator allow with"
                            " {1} already in process\n".format(
                                        self.stack.name,
                                        self.remote.name))
                    console.concise(emsg)
                    return

        self.remote.allowed = None
        if not self.remote.joined:
            emsg = "Allower {0}. Must be joined first\n".format(self.stack.name)
            console.terse(emsg)
            self.stack.incStat('unjoined_remote')
            if self.stack.local.main:
                self.stack.yoke(duid=self.remote.uid, cascade=self.cascade, timeout=self.timeout)
            else:
                self.stack.join(duid=self.remote.uid, cascade=self.cascade, timeout=self.timeout)
            return

        self.remote.rekey() # refresh short term keys and reset .allowed to None
        self.add()

        plain = binascii.hexlify("".rjust(32, '\x00'))
        cipher, nonce = self.remote.privee.encrypt(plain, self.remote.pubber.key)
        body = raeting.HELLO_PACKER.pack(plain, self.remote.privee.pubraw, cipher, nonce)

        packet = packeting.TxPacket(stack=self.stack,
                                    kind=raeting.pcktKinds.hello,
                                    embody=body,
                                    data=self.txData)
        try:
            packet.pack()
        except raeting.PacketError as ex:
            console.terse(str(ex) + '\n')
            self.stack.incStat("packing_error")
            self.remove()
            return
        self.transmit(packet)
        console.concise("Allower {0}. Do Hello with {1} at {2}\n".format(
                self.stack.name, self.remote.name, self.stack.store.stamp))

    def cookie(self):
        '''
        Process cookie packet
        '''
        if not self.stack.parseInner(self.rxPacket):
            return

        data = self.rxPacket.data
        body = self.rxPacket.body.data

        if not isinstance(body, basestring):
            emsg = "Invalid format of cookie packet body\n"
            console.terse(emsg)
            self.stack.incStat('invalid_cookie')
            #self.remove()
            self.nack(kind=raeting.pcktKinds.reject)
            return

        if len(body) != raeting.COOKIE_PACKER.size:
            emsg = "Invalid length of cookie packet body\n"
            console.terse(emsg)
            self.stack.incStat('invalid_cookie')
            #self.remove()
            self.nack(kind=raeting.pcktKinds.reject)
            return

        cipher, nonce = raeting.COOKIE_PACKER.unpack(body)

        try:
            msg = self.remote.privee.decrypt(cipher, nonce, self.remote.pubber.key)
        except ValueError as ex:
            emsg = "Invalid cookie stuff: '{0}'\n".format(str(ex))
            console.terse(emsg)
            self.stack.incStat('invalid_cookie')
            #self.remove()
            self.nack(kind=raeting.pcktKinds.reject)
            return

        if len(msg) != raeting.COOKIESTUFF_PACKER.size:
            emsg = "Invalid length of cookie stuff\n"
            console.terse(emsg)
            self.stack.incStat('invalid_cookie')
            #self.remove()
            self.nack(kind=raeting.pcktKinds.reject)
            return

        shortraw, seid, deid, oreo = raeting.COOKIESTUFF_PACKER.unpack(msg)

        if seid != self.remote.uid or deid != self.stack.local.uid:
            emsg = "Invalid seid or deid fields in cookie stuff\n"
            console.terse(emsg)
            self.stack.incStat('invalid_cookie')
            #self.remove()
            self.nack(kind=raeting.pcktKinds.reject)
            return

        self.oreo = binascii.hexlify(oreo)
        self.remote.publee = nacling.Publican(key=shortraw)

        self.initiate()

    def initiate(self):
        '''
        Send initiate request to cookie response to hello request
        '''
        vcipher, vnonce = self.stack.local.priver.encrypt(self.remote.privee.pubraw,
                                                self.remote.pubber.key)

        fqdn = self.remote.fqdn.ljust(128, ' ')

        stuff = raeting.INITIATESTUFF_PACKER.pack(self.stack.local.priver.pubraw,
                                                  vcipher,
                                                  vnonce,
                                                  fqdn)

        cipher, nonce = self.remote.privee.encrypt(stuff, self.remote.publee.key)

        oreo = binascii.unhexlify(self.oreo)
        body = raeting.INITIATE_PACKER.pack(self.remote.privee.pubraw,
                                            oreo,
                                            cipher,
                                            nonce)

        packet = packeting.TxPacket(stack=self.stack,
                                    kind=raeting.pcktKinds.initiate,
                                    embody=body,
                                    data=self.txData)
        try:
            packet.pack()
        except raeting.PacketError as ex:
            console.terse(str(ex) + '\n')
            self.stack.incStat("packing_error")
            self.remove()
            return

        self.transmit(packet)
        console.concise("Allower {0}. Do Initiate with {1} at {2}\n".format(
                self.stack.name, self.remote.name, self.stack.store.stamp))

    def allow(self):
        '''
        Process ackInitiate packet
        Perform allowment in response to ack to initiate packet
        Transmits ack to complete transaction so correspondent knows
        '''
        if not self.stack.parseInner(self.rxPacket):
            return

        self.remote.allowed = True
        self.ackFinal()

    def ackFinal(self):
        '''
        Send ack to ack Initiate to terminate transaction
        Why do we need this? could we just let transaction timeout on allowent
        '''
        body = ""
        packet = packeting.TxPacket(stack=self.stack,
                                    kind=raeting.pcktKinds.ack,
                                    embody=body,
                                    data=self.txData)
        try:
            packet.pack()
        except raeting.PacketError as ex:
            console.terse(str(ex) + '\n')
            self.stack.incStat("packing_error")
            self.remove()
            return

        self.transmit(packet)
        self.remove()
        console.concise("Allower {0}. Ack Final of {1} at {2}\n".format(
                self.stack.name, self.remote.name, self.stack.store.stamp))
        self.stack.incStat("allow_initiate_complete")

        self.remote.nextSid() # start new session
        self.remote.replaceStaleInitiators()
        self.stack.dumpRemote(self.remote)
        self.remote.sendSavedMessages() # could include messages saved on rejoin
        if self.cascade:
            self.stack.alive(duid=self.remote.uid, cascade=self.cascade, timeout=self.timeout)

    def nack(self, kind=raeting.pcktKinds.nack):
        '''
        Send nack to accept response
        '''
        body = ""
        packet = packeting.TxPacket(stack=self.stack,
                                    kind=kind,
                                    embody=body,
                                    data=self.txData)
        try:
            packet.pack()
        except raeting.PacketError as ex:
            console.terse(str(ex) + '\n')
            self.stack.incStat("packing_error")
            self.remove()
            return

        if kind == raeting.pcktKinds.refuse:
            console.terse("Allower {0}. Do Refuse of {1} at {2}\n".format(
                    self.stack.name, self.remote.name, self.stack.store.stamp))
        elif kind == raeting.pcktKinds.reject:
            console.terse("Allower {0}. Do Reject of {1} at {2}\n".format(
                    self.stack.name, self.remote.name, self.stack.store.stamp))
        elif kind == raeting.pcktKinds.nack:
            console.terse("Allower {0}. Do Nack of {1} at {2}\n".format(
                self.stack.name, self.remote.name, self.stack.store.stamp))
        else:
            console.terse("Allower {0}. Invalid nack kind of {1} nacking anyway "
                    " at {2}\n".format(self.stack.name,
                                       kind,
                                       self.stack.store.stamp))
            kind == raeting.pcktKinds.nack

        self.stack.incStat(self.statKey())
        self.transmit(packet)
        self.remove()

    def refuse(self):
        '''
        Process nack refule to packet
        '''
        if not self.stack.parseInner(self.rxPacket):
            return
        console.concise("Allower {0}. Refusted by {1} at {2}\n".format(
                self.stack.name, self.remote.name, self.stack.store.stamp))
        self.stack.incStat(self.statKey())
        self.remove()

    def reject(self):
        '''
        Process nack reject to packet
        terminate in response to nack
        '''
        if not self.stack.parseInner(self.rxPacket):
            return

        self.remote.allowed = False
        self.remove()
        console.concise("Allower {0}. Rejected by {1} at {2}\n".format(
                self.stack.name, self.remote.name, self.stack.store.stamp))
        self.stack.incStat(self.statKey())

    def unjoin(self):
        '''
        Process unjoin packet
        terminate in response to unjoin
        '''
        if not self.stack.parseInner(self.rxPacket):
            return
        self.remote.joined = False
        self.remove()
        console.concise("Allower {0}. Rejected unjoin by {1} at {2}\n".format(
                self.stack.name, self.remote.name, self.stack.store.stamp))
        self.stack.incStat(self.statKey())
        self.stack.join(duid=self.remote.uid, cascade=self.cascade, timeout=self.timeout)

class Allowent(Correspondent):
    '''
    RAET protocol Allowent Correspondent class Dual of Allower
    CurveCP handshake
    '''
    Timeout = 4.0
    RedoTimeoutMin = 0.25 # initial timeout
    RedoTimeoutMax = 1.0 # max timeout

    def __init__(self, redoTimeoutMin=None, redoTimeoutMax=None, **kwa):
        '''
        Setup instance
        '''
        kwa['kind'] = raeting.trnsKinds.allow
        super(Allowent, self).__init__(**kwa)

        self.redoTimeoutMax = redoTimeoutMax or self.RedoTimeoutMax
        self.redoTimeoutMin = redoTimeoutMin or self.RedoTimeoutMin
        self.redoTimer = aiding.StoreTimer(self.stack.store,
                                           duration=self.redoTimeoutMin)

        self.oreo = None #keep locally generated oreo around for redos
        self.prep() # prepare .txData

    def transmit(self, packet):
        '''
        Augment transmit with restart of redo timer
        '''
        super(Allowent, self).transmit(packet)
        self.redoTimer.restart()

    def receive(self, packet):
        """
        Process received packet belonging to this transaction
        """
        super(Allowent, self).receive(packet) #  self.rxPacket = packet

        if packet.data['tk'] == raeting.trnsKinds.allow:
            if packet.data['pk'] == raeting.pcktKinds.hello:
                self.hello()
            elif packet.data['pk'] == raeting.pcktKinds.initiate:
                self.initiate()
            elif packet.data['pk'] == raeting.pcktKinds.ack:
                self.final()
            elif packet.data['pk'] == raeting.pcktKinds.nack: # rejected
                self.refuse()
            elif packet.data['pk'] == raeting.pcktKinds.refuse: # refused
                self.refuse()
            elif packet.data['pk'] == raeting.pcktKinds.reject: # rejected
                self.reject()


    def process(self):
        '''
        Perform time based processing of transaction

        '''
        if self.timeout > 0.0 and self.timer.expired:
            self.nack(kind=raeting.pcktKinds.refuse)
            console.concise("Allowent {0}. Timed out with {1} at {2}\n".format(
                    self.stack.name, self.remote.name, self.stack.store.stamp))
            return

        # need to perform the check for accepted status and then send accept
        if self.redoTimer.expired:
            duration = min(
                         max(self.redoTimeoutMin,
                              self.redoTimer.duration * 2.0),
                         self.redoTimeoutMax)
            self.redoTimer.restart(duration=duration)

            if self.txPacket:
                if self.txPacket.data['pk'] == raeting.pcktKinds.cookie:
                    self.transmit(self.txPacket) #redo
                    console.concise("Allowent {0}. Redo Cookie with {1} at {2}\n".format(
                             self.stack.name, self.remote.name, self.stack.store.stamp))
                    self.stack.incStat('redo_cookie')

                if self.txPacket.data['pk'] == raeting.pcktKinds.ack:
                    self.transmit(self.txPacket) #redo
                    console.concise("Allowent {0}. Redo Ack with {1} at {2}\n".format(
                             self.stack.name, self.remote.name, self.stack.store.stamp))
                    self.stack.incStat('redo_allow')

    def prep(self):
        '''
        Prepare .txData
        '''
        self.txData.update( sh=self.stack.local.host,
                            sp=self.stack.local.port,
                            dh=self.remote.host,
                            dp=self.remote.port,
                            se=self.stack.local.uid,
                            de=self.remote.uid,
                            tk=self.kind,
                            cf=self.rmt,
                            bf=self.bcst,
                            wf=self.wait,
                            si=self.sid,
                            ti=self.tid, )

    def hello(self):
        '''
        Process hello packet
        '''
        if not self.stack.parseInner(self.rxPacket):
            return

        allows = self.remote.allowInProcess()
        if allows:
            if not self.stack.local.main:
                emsg = "Allowent {0}. Allow with {1} already in process\n".format(
                        self.stack.name, self.remote.name)
                console.concise(emsg)
                self.stack.incStat('duplicate_allow_attempt')
                self.nack(kind=raeting.pcktKinds.refuse)
                return
            else: # main so remove any initiator allows
                already = False
                for allow in allows:
                    if not allow.rmt:
                        emsg = ("Allower {0}. Removing initiator allow with"
                                " {1} already in process\n".format(
                                            self.stack.name,
                                            self.remote.name))
                        console.concise(emsg)
                        allow.nack(kind=raeting.pcktKinds.refuse)
                    else: # already correspondent
                        already = True
                if already:
                    emsg = ("Allower {0}. Correspondent allow with"
                            " {1} already in process\n".format(
                                        self.stack.name,
                                        self.remote.name))
                    console.concise(emsg)
                    return

        self.remote.allowed = None

        if not self.remote.joined:
            emsg = "Allowent {0}. Must be joined with {1} first\n".format(
                self.stack.name, self.remote.name)
            console.terse(emsg)
            self.stack.incStat('unjoined_allow_attempt')
            self.nack(kind=raeting.pcktKinds.unjoined)
            return

        self.remote.rekey() # refresh short term keys and .allowed
        self.add()

        data = self.rxPacket.data
        body = self.rxPacket.body.data

        if not isinstance(body, basestring):
            emsg = "Invalid format of hello packet body\n"
            console.terse(emsg)
            self.stack.incStat('invalid_hello')
            #self.remove()
            self.nack(kind=raeting.pcktKinds.reject)
            return

        if len(body) != raeting.HELLO_PACKER.size:
            emsg = "Invalid length of hello packet body\n"
            console.terse(emsg)
            self.stack.incStat('invalid_hello')
            #self.remove()
            self.nack(kind=raeting.pcktKinds.reject)
            return

        plain, shortraw, cipher, nonce = raeting.HELLO_PACKER.unpack(body)

        self.remote.publee = nacling.Publican(key=shortraw)
        msg = self.stack.local.priver.decrypt(cipher, nonce, self.remote.publee.key)
        if msg != plain :
            emsg = "Invalid plain not match decrypted cipher\n"
            console.terse(emsg)
            self.stack.incStat('invalid_hello')
            #self.remove()
            self.nack(kind=raeting.pcktKinds.reject)
            return

        self.cookie()

    def cookie(self):
        '''
        Send Cookie Packet
        '''
        oreo = self.stack.local.priver.nonce()
        self.oreo = binascii.hexlify(oreo)

        stuff = raeting.COOKIESTUFF_PACKER.pack(self.remote.privee.pubraw,
                                                self.stack.local.uid,
                                                self.remote.uid,
                                                oreo)

        cipher, nonce = self.stack.local.priver.encrypt(stuff, self.remote.publee.key)
        body = raeting.COOKIE_PACKER.pack(cipher, nonce)
        packet = packeting.TxPacket(stack=self.stack,
                                    kind=raeting.pcktKinds.cookie,
                                    embody=body,
                                    data=self.txData)
        try:
            packet.pack()
        except raeting.PacketError as ex:
            console.terse(str(ex) + '\n')
            self.stack.incStat("packing_error")
            self.remove()
            return
        self.transmit(packet)
        console.concise("Allowent {0}. Do Cookie with {1} at {2}\n".format(
                self.stack.name, self.remote.name, self.stack.store.stamp))

    def initiate(self):
        '''
        Process initiate packet
        '''
        if not self.stack.parseInner(self.rxPacket):
            return
        data = self.rxPacket.data
        body = self.rxPacket.body.data

        if not isinstance(body, basestring):
            emsg = "Invalid format of initiate packet body\n"
            console.terse(emsg)
            self.stack.incStat('invalid_initiate')
            #self.remove()
            self.nack(kind=raeting.pcktKinds.reject)
            return

        if len(body) != raeting.INITIATE_PACKER.size:
            emsg = "Invalid length of initiate packet body\n"
            console.terse(emsg)
            self.stack.incStat('invalid_initiate')
            #self.remove()
            self.nack(kind=raeting.pcktKinds.reject)
            return

        shortraw, oreo, cipher, nonce = raeting.INITIATE_PACKER.unpack(body)

        if shortraw != self.remote.publee.keyraw:
            emsg = "Mismatch of short term public key in initiate packet\n"
            console.terse(emsg)
            self.stack.incStat('invalid_initiate')
            #self.remove()
            self.nack(kind=raeting.pcktKinds.reject)
            return

        if (binascii.hexlify(oreo) != self.oreo):
            emsg = "Stale or invalid cookie in initiate packet\n"
            console.terse(emsg)
            self.stack.incStat('invalid_initiate')
            #self.remove()
            self.nack(kind=raeting.pcktKinds.reject)
            return

        msg = self.remote.privee.decrypt(cipher, nonce, self.remote.publee.key)
        if len(msg) != raeting.INITIATESTUFF_PACKER.size:
            emsg = "Invalid length of initiate stuff\n"
            console.terse(emsg)
            self.stack.incStat('invalid_initiate')
            #self.remove()
            self.nack(kind=raeting.pcktKinds.reject)
            return

        pubraw, vcipher, vnonce, fqdn = raeting.INITIATESTUFF_PACKER.unpack(msg)
        if pubraw != self.remote.pubber.keyraw:
            emsg = "Mismatch of long term public key in initiate stuff\n"
            console.terse(emsg)
            self.stack.incStat('invalid_initiate')
            #self.remove()
            self.nack(kind=raeting.pcktKinds.reject)
            return

        fqdn = fqdn.rstrip(' ')
        if fqdn != self.stack.local.fqdn:
            emsg = "Mismatch of fqdn in initiate stuff\n"
            console.terse(emsg)
            #self.stack.incStat('invalid_initiate')
            #self.remove()
            #self.nack(kind=raeting.pcktKinds.reject)
            #return

        vouch = self.stack.local.priver.decrypt(vcipher, vnonce, self.remote.pubber.key)
        if vouch != self.remote.publee.keyraw or vouch != shortraw:
            emsg = "Short term key vouch failed\n"
            console.terse(emsg)
            self.stack.incStat('invalid_initiate')
            #self.remove()
            self.nack(kind=raeting.pcktKinds.reject)
            return

        self.ackInitiate()

    def ackInitiate(self):
        '''
        Send ack to initiate request
        '''

        body = ""
        packet = packeting.TxPacket(stack=self.stack,
                                    kind=raeting.pcktKinds.ack,
                                    embody=body,
                                    data=self.txData)
        try:
            packet.pack()
        except raeting.PacketError as ex:
            console.terse(str(ex) + '\n')
            self.stack.incStat("packing_error")
            self.remove()
            return

        self.transmit(packet)
        console.concise("Allowent {0}. Do Ack with {1} at {2}\n".format(
                self.stack.name, self.remote.name, self.stack.store.stamp))

        self.allow()

    def allow(self):
        '''
        Perform allowment
        '''
        self.remote.allowed = True
        self.remote.nextSid() # start new session
        self.remote.replaceStaleInitiators()
        self.stack.dumpRemote(self.remote)

    def final(self):
        '''
        Process ackFinal packet
        So that both sides are waiting on acks at the end so does not restart
        transaction if ack initiate is dropped
        '''
        if not self.stack.parseInner(self.rxPacket):
            return

        self.remove()
        console.concise("Allowent {0}. Do Final with {1} at {2}\n".format(
                self.stack.name, self.remote.name, self.stack.store.stamp))
        self.stack.incStat("allow_correspond_complete")
        self.remote.sendSavedMessages() # could include messages saved on rejoin

    def refuse(self):
        '''
        Process nack refuse packet
        '''
        if not self.stack.parseInner(self.rxPacket):
            return

        self.remove()
        console.concise("Allowent {0}. Refused by {1} at {2}\n".format(
                self.stack.name, self.remote.name, self.stack.store.stamp))
        self.stack.incStat(self.statKey())

    def reject(self):
        '''
        Process nack packet
        terminate in response to nack
        '''
        if not self.stack.parseInner(self.rxPacket):
            return

        self.remote.allowed = False
        self.remove()
        console.concise("Allowent {0}. Rejected by {1} at {2}\n".format(
                self.stack.name, self.remote.name, self.stack.store.stamp))
        self.stack.incStat(self.statKey())

    def nack(self, kind=raeting.pcktKinds.nack):
        '''
        Send nack to terminate allow transaction
        '''
        body = ""
        packet = packeting.TxPacket(stack=self.stack,
                                    kind=kind,
                                    embody=body,
                                    data=self.txData)
        try:
            packet.pack()
        except raeting.PacketError as ex:
            console.terse(str(ex) + '\n')
            self.stack.incStat("packing_error")
            self.remove()
            return

        if kind==raeting.pcktKinds.refuse:
            console.terse("Allowent {0}. Do Refuse of {1} at {2}\n".format(
                    self.stack.name, self.remote.name, self.stack.store.stamp))
        elif kind==raeting.pcktKinds.reject:
            console.concise("Allowent {0}. Do Reject {1} at {2}\n".format(
                    self.stack.name, self.remote.name, self.stack.store.stamp))
        elif kind == raeting.pcktKinds.nack:
            console.terse("Allowent {0}. Do Nack of {1} at {2}\n".format(
                    self.stack.name, self.remote.name, self.stack.store.stamp))
        else:
            console.terse("Allowent {0}. Invalid nack kind of {1} nacking anyway "
                    " at {2}\n".format(self.stack.name,
                                       kind,
                                       self.stack.store.stamp))
            kind == raeting.pcktKinds.nack
        self.transmit(packet)
        self.remove()
        self.stack.incStat(self.statKey())

class Aliver(Initiator):
    '''
    RAET protocol Aliver Initiator class Dual of Alivent
    Sends keep alive heatbeat messages to detect presence


    update alived status of .remote
    only use .remote.refresh to update

    '''
    Timeout = 2.0
    RedoTimeoutMin = 0.25 # initial timeout
    RedoTimeoutMax = 1.0 # max timeout

    def __init__(self, redoTimeoutMin=None, redoTimeoutMax=None,
                cascade=False, **kwa):
        '''
        Setup instance
        '''
        kwa['kind'] = raeting.trnsKinds.alive
        super(Aliver, self).__init__(**kwa)

        self.cascade = cascade

        self.redoTimeoutMax = redoTimeoutMax or self.RedoTimeoutMax
        self.redoTimeoutMin = redoTimeoutMin or self.RedoTimeoutMin
        self.redoTimer = aiding.StoreTimer(self.stack.store,
                                           duration=self.redoTimeoutMin)

        self.sid = self.remote.sid
        self.tid = self.remote.nextTid()
        self.prep() # prepare .txData

    def transmit(self, packet):
        '''
        Augment transmit with restart of redo timer
        '''
        super(Aliver, self).transmit(packet)
        self.redoTimer.restart()

    def receive(self, packet):
        """
        Process received packet belonging to this transaction
        """
        super(Aliver, self).receive(packet)

        if packet.data['tk'] == raeting.trnsKinds.alive:
            if packet.data['pk'] == raeting.pcktKinds.ack:
                self.complete()
            elif packet.data['pk'] == raeting.pcktKinds.nack: # refused
                self.refuse()
            elif packet.data['pk'] == raeting.pcktKinds.refuse: # refused
                self.refuse()
            elif packet.data['pk'] == raeting.pcktKinds.unjoined: # unjoin
                self.unjoin()
            elif packet.data['pk'] == raeting.pcktKinds.unallowed: # unallow
                self.unallow()
            elif packet.data['pk'] == raeting.pcktKinds.reject: # rejected
                self.reject()

    def process(self):
        '''
        Perform time based processing of transaction
        '''
        if self.timeout > 0.0 and self.timer.expired:
            console.concise("Aliver {0}. Timed out with {1} at {2}\n".format(
                self.stack.name, self.remote.name, self.stack.store.stamp))
            self.remove()
            self.remote.refresh(alived=False) # mark as dead
            return

        # need keep sending message until completed or timed out
        if self.redoTimer.expired:
            duration = min(
                         max(self.redoTimeoutMin,
                              self.redoTimer.duration * 2.0),
                         self.redoTimeoutMax)
            self.redoTimer.restart(duration=duration)
            if self.txPacket:
                if self.txPacket.data['pk'] == raeting.pcktKinds.request:
                    self.transmit(self.txPacket) # redo
                    console.concise("Aliver {0}. Redo with {1} at {2}\n".format(
                        self.stack.name, self.remote.name, self.stack.store.stamp))
                    self.stack.incStat('redo_alive')

    def prep(self):
        '''
        Prepare .txData
        '''
        self.txData.update( sh=self.stack.local.host,
                            sp=self.stack.local.port,
                            dh=self.remote.host,
                            dp=self.remote.port,
                            se=self.stack.local.uid,
                            de=self.remote.uid,
                            tk=self.kind,
                            cf=self.rmt,
                            bf=self.bcst,
                            wf=self.wait,
                            si=self.sid,
                            ti=self.tid,)

    def alive(self, body=None):
        '''
        Send message
        '''
        if not self.remote.joined:
            emsg = "Aliver {0}. Must be joined with {1} first\n".format(
                    self.stack.name, self.remote.name)
            console.terse(emsg)
            self.stack.incStat('unjoined_remote')
            if self.stack.local.main:
                self.stack.yoke(duid=self.remote.uid, cascade=self.cascade, timeout=self.timeout)
            else:
                self.stack.join(duid=self.remote.uid, cascade=self.cascade, timeout=self.timeout)
            return

        if not self.remote.allowed:
            emsg = "Aliver {0}. Must be allowed with {1} first\n".format(
                    self.stack.name, self.remote.name)
            console.terse(emsg)
            self.stack.incStat('unallowed_remote')
            self.stack.allow(duid=self.remote.uid, cascade=self.cascade, timeout=self.timeout)
            return

        self.remote.refresh(alived=None) #Restart timer but do not change alived status
        self.add()

        body = odict()
        packet = packeting.TxPacket(stack=self.stack,
                                    kind=raeting.pcktKinds.request,
                                    embody=body,
                                    data=self.txData)
        try:
            packet.pack()
        except raeting.PacketError as ex:
            console.terse(str(ex) + '\n')
            self.stack.incStat("packing_error")
            self.remove()
            return
        self.transmit(packet)
        console.concise("Aliver {0}. Do Alive with {1} at {2}\n".format(
                self.stack.name, self.remote.name, self.stack.store.stamp))

    def complete(self):
        '''
        Process ack packet. Complete transaction and remove
        '''
        if not self.stack.parseInner(self.rxPacket):
            return
        self.remote.refresh(alived=True) # restart timer mark as alive
        self.remove()
        console.concise("Aliver {0}. Done with {1} at {2}\n".format(
                self.stack.name, self.remote.name, self.stack.store.stamp))
        self.stack.incStat("alive_complete")

    def refuse(self):
        '''
        Process nack refuse packet
        terminate in response to nack
        '''
        if not self.stack.parseInner(self.rxPacket):
            return
        self.remote.refresh(alived=None) # restart timer do not change status
        self.remove()
        console.concise("Aliver {0}. Refused by {1} at {2}\n".format(
                self.stack.name, self.remote.name, self.stack.store.stamp))
        self.stack.incStat(self.statKey())

    def reject(self):
        '''
        Process nack reject packet
        terminate in response to nack
        '''
        if not self.stack.parseInner(self.rxPacket):
            return
        self.remote.refresh(alived=False) # restart timer set status to False
        self.remove()
        console.concise("Aliver {0}. Rejected by {1} at {2}\n".format(
                self.stack.name, self.remote.name, self.stack.store.stamp))
        self.stack.incStat(self.statKey())

    def unjoin(self):
        '''
        Process unjoin packet
        terminate in response to unjoin
        '''
        if not self.stack.parseInner(self.rxPacket):
            return
        self.remote.refresh(alived=None) # restart timer do not change status
        self.remote.joined = False
        self.remove()
        console.concise("Aliver {0}. Refused unjoin by {1} at {2}\n".format(
                self.stack.name, self.remote.name, self.stack.store.stamp))
        self.stack.incStat(self.statKey())
        self.stack.join(duid=self.remote.uid, cascade=self.cascade, timeout=self.timeout)

    def unallow(self):
        '''
        Process unallow nack packet
        terminate in response to unallow
        '''
        if not self.stack.parseInner(self.rxPacket):
            return
        self.remote.refresh(alived=None) # restart timer do not change status
        self.remote.allowed = False
        self.remove()
        console.concise("Aliver {0}. Refused unallow by {1} at {2}\n".format(
                self.stack.name, self.remote.name, self.stack.store.stamp))
        self.stack.incStat(self.statKey())
        self.stack.allow(duid=self.remote.uid, cascade=self.cascade, timeout=self.timeout)

class Alivent(Correspondent):
    '''
    RAET protocol Alivent Correspondent class Dual of Aliver
    Keep alive heartbeat
    '''
    Timeout = 10.0

    def __init__(self, **kwa):
        '''
        Setup instance
        '''
        kwa['kind'] = raeting.trnsKinds.alive
        super(Alivent, self).__init__(**kwa)

        self.prep() # prepare .txData

    def receive(self, packet):
        """
        Process received packet belonging to this transaction
        """
        super(Alivent, self).receive(packet)

        if packet.data['tk'] == raeting.trnsKinds.alive:
            if packet.data['pk'] == raeting.pcktKinds.request:
                self.alive()

    def process(self):
        '''
        Perform time based processing of transaction

        '''
        if self.timeout > 0.0 and self.timer.expired:
            self.nack() #manage restarts alive later
            console.concise("Alivent {0}. Timed out with {1} at {2}\n".format(
                    self.stack.name, self.remote.name, self.stack.store.stamp))
            return

    def prep(self):
        '''
        Prepare .txData
        '''
        self.txData.update( sh=self.stack.local.host,
                            sp=self.stack.local.port,
                            dh=self.remote.host,
                            dp=self.remote.port,
                            se=self.stack.local.uid,
                            de=self.remote.uid,
                            tk=self.kind,
                            cf=self.rmt,
                            bf=self.bcst,
                            wf=self.wait,
                            si=self.sid,
                            ti=self.tid,)

    def alive(self):
        '''
        Process alive packet
        '''
        if not self.stack.parseInner(self.rxPacket):
            return

        if not self.remote.joined:
            self.remote.refresh(alived=None) # received signed packet so its alive
            emsg = "Alivent {0}. Must be joined with {1} first\n".format(
                    self.stack.name, self.remote.name)
            console.terse(emsg)
            self.stack.incStat('unjoined_alive_attempt')
            self.nack(kind=raeting.pcktKinds.unjoined)
            return

        if not self.remote.allowed:
            self.remote.refresh(alived=None) # received signed packet so its alive
            emsg = "Alivent {0}. Must be allowed with {1} first\n".format(
                    self.stack.name, self.remote.name)
            console.terse(emsg)
            self.stack.incStat('unallowed_alive_attempt')
            self.nack(kind=raeting.pcktKinds.unallowed)
            return

        self.add()

        data = self.rxPacket.data
        body = self.rxPacket.body.data

        body = odict()
        packet = packeting.TxPacket(stack=self.stack,
                                    kind=raeting.pcktKinds.ack,
                                    embody=body,
                                    data=self.txData)
        try:
            packet.pack()
        except raeting.PacketError as ex:
            console.terse(str(ex) + '\n')
            self.stack.incStat("packing_error")
            self.remove(index=self.rxPacket.index)
            return

        self.transmit(packet)
        console.concise("Alivent {0}. Do ack alive with {1} at {2}\n".format(
                self.stack.name, self.remote.name, self.stack.store.stamp))
        self.remote.refresh(alived=True)
        self.remove()
        console.concise("Alivent {0}. Done with {1} at {2}\n".format(
                self.stack.name, self.remote.name, self.stack.store.stamp))
        self.stack.incStat("alive_complete")

    def nack(self, kind=raeting.pcktKinds.nack):
        '''
        Send nack to terminate alive transaction
        '''
        body = odict()
        packet = packeting.TxPacket(stack=self.stack,
                                    kind=kind,
                                    embody=body,
                                    data=self.txData)
        try:
            packet.pack()
        except raeting.PacketError as ex:
            console.terse(str(ex) + '\n')
            self.stack.incStat("packing_error")
            self.remove()
            return

        if kind == raeting.pcktKinds.refuse:
                console.terse("Alivent {0}. Do Refuse of {1} at {2}\n".format(
                        self.stack.name, self.remote.name, self.stack.store.stamp))
        elif kind == raeting.pcktKinds.unjoined:
                console.terse("Alivent {0}. Do Unjoined of {1} at {2}\n".format(
                        self.stack.name, self.remote.name, self.stack.store.stamp))
        elif kind == raeting.pcktKinds.unallowed:
                console.terse("Alivent {0}. Do Unallowed of {1} at {2}\n".format(
                        self.stack.name, self.remote.name, self.stack.store.stamp))
        elif kind == raeting.pcktKinds.reject:
            console.concise("Alivent {0}. Do Reject {1} at {2}\n".format(
                    self.stack.name, self.remote.name, self.stack.store.stamp))
        elif kind == raeting.pcktKinds.nack:
            console.terse("Alivent {0}. Do Nack of {1} at {2}\n".format(
                    self.stack.name, self.remote.name, self.stack.store.stamp))
        else:
            console.terse("Alivent {0}. Invalid nack kind of {1} nacking anyway "
                    " at {2}\n".format(self.stack.name,
                                       kind,
                                       self.stack.store.stamp))
            kind == raeting.pcktKinds.nack

        self.transmit(packet)
        self.remove()

        self.stack.incStat(self.statKey())

class Messenger(Initiator):
    '''
    RAET protocol Messenger Initiator class Dual of Messengent
    Generic messages
    '''
    Timeout = 10.0
    RedoTimeoutMin = 1.0 # initial timeout
    RedoTimeoutMax = 3.0 # max timeout

    def __init__(self, redoTimeoutMin=None, redoTimeoutMax=None, **kwa):
        '''
        Setup instance
        '''
        kwa['kind'] = raeting.trnsKinds.message
        super(Messenger, self).__init__(**kwa)

        self.redoTimeoutMax = redoTimeoutMax or self.RedoTimeoutMax
        self.redoTimeoutMin = redoTimeoutMin or self.RedoTimeoutMin
        self.redoTimer = aiding.StoreTimer(self.stack.store,
                                           duration=self.redoTimeoutMin)

        self.sid = self.remote.sid
        self.tid = self.remote.nextTid()
        self.prep() # prepare .txData
        self.tray = packeting.TxTray(stack=self.stack)

    def transmit(self, packet):
        '''
        Augment transmit with restart of redo timer
        '''
        super(Messenger, self).transmit(packet)
        self.redoTimer.restart()

    def receive(self, packet):
        """
        Process received packet belonging to this transaction
        """
        super(Messenger, self).receive(packet)

        if packet.data['tk'] == raeting.trnsKinds.message:
            if packet.data['pk'] == raeting.pcktKinds.ack:
                self.another()
            elif packet.data['pk'] == raeting.pcktKinds.nack: # rejected
                self.reject()
            elif packet.data['pk'] == raeting.pcktKinds.resend: # missed resend
                self.resend()

    def process(self):
        '''
        Perform time based processing of transaction
        '''
        if self.timeout > 0.0 and self.timer.expired:
            self.remove()
            console.concise("Messenger {0}. Timed out with {1} at {2}\n".format(
                    self.stack.name, self.remote.name, self.stack.store.stamp))
            return

        # need keep sending message until completed or timed out
        if self.redoTimer.expired:
            duration = min(
                         max(self.redoTimeoutMin,
                              self.redoTimer.duration * 2.0),
                         self.redoTimeoutMax)
            self.redoTimer.restart(duration=duration)
            if self.txPacket:
                if self.txPacket.data['pk'] == raeting.pcktKinds.message:
                    self.transmit(self.txPacket) # redo
                    console.concise("Messenger {0}. Redo Segment {1} with {2} at {3}\n".format(
                            self.stack.name, self.tray.last, self.remote.name, self.stack.store.stamp))
                    self.stack.incStat('redo_segment')

    def prep(self):
        '''
        Prepare .txData
        '''
        self.txData.update( sh=self.stack.local.host,
                            sp=self.stack.local.port,
                            dh=self.remote.host,
                            dp=self.remote.port,
                            se=self.stack.local.uid,
                            de=self.remote.uid,
                            tk=self.kind,
                            cf=self.rmt,
                            bf=self.bcst,
                            wf=self.wait,
                            si=self.sid,
                            ti=self.tid,)

    def message(self, body=None):
        '''
        Send message or part of message. So repeatedly called untill complete
        '''

        if not self.remote.allowed:
            emsg = "Messenger {0}. Must be allowed with {1} first\n".format(
                    self.stack.name, self.remote.name)
            console.terse(emsg)
            self.stack.incStat('unallowed_remote')
            self.remove()
            return

        if not self.tray.packets:
            try:
                self.tray.pack(data=self.txData, body=body)
            except raeting.PacketError as ex:
                console.terse(str(ex) + '\n')
                self.stack.incStat("packing_error")
                self.remove()
                return

        if self.tray.current >= len(self.tray.packets):
            emsg = "Messenger {0}. Current packet {1} greater than num packets {2}\n".format(
                                self.stack.name, self.tray.current, len(self.tray.packets))
            console.terse(emsg)
            self.remove()
            return

        if self.index not in self.remote.transactions:
            self.add()
        elif self.remote.transactions[self.index] != self:
            emsg = "Messenger {0}. Remote {1} Index collision at {2}\n".format(
                                self.stack.name, self.remote.name,  self.index)
            console.terse(emsg)
            self.incStat('message_index_collision')
            self.remove()
            return

        burst = 1 if self.wait else len(self.tray.packets) - self.tray.current

        for packet in self.tray.packets[self.tray.current:self.tray.current + burst]:
            self.transmit(packet) #if self.tray.current %  2 else None
            self.tray.last = self.tray.current
            self.stack.incStat("message_segment_tx")
            console.concise("Messenger {0}. Do Message Segment {1} with {2} at {3}\n".format(
                    self.stack.name, self.tray.last, self.remote.name, self.stack.store.stamp))
            self.tray.current += 1

    def another(self):
        '''
        Process ack packet send next one
        '''
        if not self.stack.parseInner(self.rxPacket):
            return

        self.remote.refresh(alived=True)

        if self.tray.current >= len(self.tray.packets):
            self.complete()
        else:
            self.message()

    def resend(self):
        '''
        Process resend packet and send misseds list of missing packets
        '''
        if not self.stack.parseInner(self.rxPacket):
            return

        self.remote.refresh(alived=True)

        data = self.rxPacket.data
        body = self.rxPacket.body.data

        misseds = body.get('misseds')
        if misseds:
            if not self.tray.packets:
                emsg = "Invalid resend request '{0}'\n".format(misseds)
                console.terse(emsg)
                self.stack.incStat('invalid_resend')
                return

            for m in misseds:
                try:
                    packet = self.tray.packets[m]
                except IndexError as ex:
                    #console.terse(str(ex) + '\n')
                    console.terse("Invalid misseds segment number {0}\n".format(m))
                    self.stack.incStat("invalid_misseds")
                    return

                self.transmit(packet)
                self.stack.incStat("message_segment_tx")
                console.concise("Messenger {0}. Resend Message Segment {1} with {2} at {3}\n".format(
                        self.stack.name, m, self.remote.name, self.stack.store.stamp))

    def complete(self):
        '''
        Complete transaction and remove
        '''
        self.remove()
        console.concise("Messenger {0}. Done with {1} at {2}\n".format(
                self.stack.name, self.remote.name, self.stack.store.stamp))
        self.stack.incStat("message_initiate_complete")

    def reject(self):
        '''
        Process nack packet
        terminate in response to nack
        '''
        if not self.stack.parseInner(self.rxPacket):
            return

        self.remote.refresh(alived=True)

        self.remove()
        console.concise("Messenger {0}. Rejected by {1} at {2}\n".format(
                self.stack.name, self.remote.name, self.stack.store.stamp))
        self.stack.incStat(self.statKey())

    def nack(self):
        '''
        Send nack to terminate transaction
        '''
        body = odict()
        packet = packeting.TxPacket(stack=self.stack,
                                    kind=raeting.pcktKinds.nack,
                                    embody=body,
                                    data=self.txData)
        try:
            packet.pack()
        except raeting.PacketError as ex:
            console.terse(str(ex) + '\n')
            self.stack.incStat("packing_error")
            self.remove()
            return

        self.transmit(packet)
        self.remove()
        console.concise("Messenger {0}. Do Reject {1} at {2}\n".format(
                self.stack.name, self.remote.name, self.stack.store.stamp))
        self.stack.incStat(self.statKey())

class Messengent(Correspondent):
    '''
    RAET protocol Messengent Correspondent class Dual of Messenger
    Generic Messages
    '''
    Timeout = 10.0
    RedoTimeoutMin = 1.0 # initial timeout
    RedoTimeoutMax = 3.0 # max timeout

    def __init__(self, redoTimeoutMin=None, redoTimeoutMax=None, **kwa):
        '''
        Setup instance
        '''
        kwa['kind'] = raeting.trnsKinds.message
        super(Messengent, self).__init__(**kwa)

        self.redoTimeoutMax = redoTimeoutMax or self.RedoTimeoutMax
        self.redoTimeoutMin = redoTimeoutMin or self.RedoTimeoutMin
        self.redoTimer = aiding.StoreTimer(self.stack.store,
                                           duration=self.redoTimeoutMin)

        self.prep() # prepare .txData
        self.tray = packeting.RxTray(stack=self.stack)

    def transmit(self, packet):
        '''
        Augment transmit with restart of redo timer
        '''
        super(Messengent, self).transmit(packet)
        self.redoTimer.restart()

    def receive(self, packet):
        """
        Process received packet belonging to this transaction
        """
        super(Messengent, self).receive(packet)

        # resent message
        if packet.data['tk'] == raeting.trnsKinds.message:
            if packet.data['pk'] == raeting.pcktKinds.message:
                self.message()
            elif packet.data['pk'] == raeting.pcktKinds.nack: # rejected
                self.reject()

    def process(self):
        '''
        Perform time based processing of transaction

        '''
        if self.timeout > 0.0 and self.timer.expired:
            self.nack()
            console.concise("Messengent {0}. Timed out with {1} at {2}\n".format(
                    self.stack.name, self.remote.name, self.stack.store.stamp))
            return

        if self.redoTimer.expired:
            duration = min(
                         max(self.redoTimeoutMin,
                              self.redoTimer.duration * 2.0),
                         self.redoTimeoutMax)
            self.redoTimer.restart(duration=duration)

            misseds = self.tray.missing()
            if misseds:
                self.resend(misseds)

    def prep(self):
        '''
        Prepare .txData
        '''
        self.txData.update( sh=self.stack.local.host,
                            sp=self.stack.local.port,
                            dh=self.remote.host,
                            dp=self.remote.port,
                            se=self.stack.local.uid,
                            de=self.remote.uid,
                            tk=self.kind,
                            cf=self.rmt,
                            bf=self.bcst,
                            wf=self.wait,
                            si=self.sid,
                            ti=self.tid,)

    def message(self):
        '''
        Process message packet. Called repeatedly for each packet in message
        '''
        if not self.remote.allowed:
            emsg = "Messengent {0}. Must be allowed with {1} first\n".format(
                    self.stack.name,  self.remote.name)
            console.terse(emsg)
            self.stack.incStat('unallowed_message_attempt')
            self.nack()
            return

        try:
            body = self.tray.parse(self.rxPacket)
        except raeting.PacketError as ex:
            console.terse(str(ex) + '\n')
            self.incStat('parsing_message_error')
            self.nack()
            return

        if self.index not in self.remote.transactions:
            self.add()
        elif self.remote.transactions[self.index] != self:
            emsg = "Messengent {0}. Remote {1} Index collision at {2}\n".format(
                                self.stack.name, self.remote.name, self.index)
            console.terse(emsg)
            self.incStat('message_index_collision')
            self.nack()
            return

        self.remote.refresh(alived=True)

        self.stack.incStat("message_segment_rx")

        if self.tray.complete:
            self.ackMessage()
            console.verbose("{0} received message body\n{1}\n".format(
                    self.stack.name, body))
            # application layer authorizaiton needs to know who sent the message
            self.stack.rxMsgs.append((body, self.remote.name))
            self.complete()

        elif self.wait:
            self.ackMessage()

        else:
            misseds = self.tray.missing(begin=self.tray.prev, end=self.tray.last)
            if misseds:
                self.resend(misseds)

    def ackMessage(self):
        '''
        Send ack to message
        '''
        body = odict()
        packet = packeting.TxPacket(stack=self.stack,
                                    kind=raeting.pcktKinds.ack,
                                    embody=body,
                                    data=self.txData)
        try:
            packet.pack()
        except raeting.PacketError as ex:
            console.terse(str(ex) + '\n')
            self.stack.incStat("packing_error")
            self.remove()
            return
        self.transmit(packet)
        self.stack.incStat("message_segment_ack")
        console.concise("Messengent {0}. Do Ack Segment {1} with {2} at {3}\n".format(
                self.stack.name, self.tray.last, self.remote.name, self.stack.store.stamp))

    def resend(self, misseds):
        '''
        Send resend request(s) for missing packets
        '''
        while misseds:
            if len(misseds) > 64:
                remainders = misseds[64:] # only do at most 64 at a time
                misseds = misseds[:64]
            else:
                remainders = []

            body = odict(misseds=misseds)
            packet = packeting.TxPacket(stack=self.stack,
                                        kind=raeting.pcktKinds.resend,
                                        embody=body,
                                        data=self.txData)
            try:
                packet.pack()
            except raeting.PacketError as ex:
                console.terse(str(ex) + '\n')
                self.stack.incStat("packing_error")
                self.remove()
                return
            self.transmit(packet)
            self.stack.incStat("message_resend")
            console.concise("Messengent {0}. Do Resend Segments {1} with {2} at {3}\n".format(
                    self.stack.name, misseds, self.remote.name, self.stack.store.stamp))
            misseds = remainders

    def complete(self):
        '''
        Complete transaction and remove
        '''
        self.remove()
        console.concise("Messengent {0}. Complete with {1} at {2}\n".format(
                self.stack.name, self.remote.name, self.stack.store.stamp))
        self.stack.incStat("messagent_correspond_complete")

    def reject(self):
        '''
        Process nack packet
        terminate in response to nack
        '''
        if not self.stack.parseInner(self.rxPacket):
            return

        self.remote.refresh(alived=True)

        self.remove()
        console.concise("Messengent {0}. Rejected by {1} at {2}\n".format(
                self.stack.name, self.remote.name, self.stack.store.stamp))
        self.stack.incStat(self.statKey())

    def nack(self):
        '''
        Send nack to terminate messenger transaction
        '''
        body = odict()
        packet = packeting.TxPacket(stack=self.stack,
                                    kind=raeting.pcktKinds.nack,
                                    embody=body,
                                    data=self.txData)
        try:
            packet.pack()
        except raeting.PacketError as ex:
            console.terse(str(ex) + '\n')
            self.stack.incStat("packing_error")
            self.remove()
            return

        self.transmit(packet)
        self.remove()
        console.concise("Messagent {0}. Do Reject {1} at {2}\n".format(
                self.stack.name, self.remote.name, self.stack.store.stamp))
        self.stack.incStat(self.statKey())

