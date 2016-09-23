import logging
from thespian.actors import *
from thespian.system.utilis import (thesplog, ExpiryTime,
                                    checkActorCapabilities,
                                    foldl, join, fmap,
                                    AssocList,
                                    actualActorClass)
from thespian.system.logdirector import LogAggregator
from thespian.system.admin.globalNames import GlobalNamesAdmin
from thespian.system.admin.adminCore import ValidSource
from thespian.system.transport import TransmitIntent, ReceiveEnvelope
from thespian.system.messages.admin import PendingActorResponse
from thespian.system.messages.convention import *
from thespian.system.sourceLoader import loadModuleFromHashSource
from thespian.system.transport.hysteresis import HysteresisDelaySender
from functools import partial
from datetime import timedelta


CONVENTION_REREGISTRATION_PERIOD  = timedelta(minutes=7, seconds=22)
CONVENTION_RESTART_PERIOD         = timedelta(minutes=3, seconds=22)
CONVENTION_REGISTRATION_MISS_MAX  = 3  # # of missing convention registrations before death declared


class PreRegistration(object):

    def __init__(self):
        self.pingValid   = ExpiryTime(timedelta(seconds=0))
        self.pingPending = False

    def refresh(self):
        self.pingValid = ExpiryTime(CONVENTION_REREGISTRATION_PERIOD)


class ConventionMemberData(object):
    def __init__(self, address, capabilities, preRegOnly=False):
        self.remoteAddress      = address
        self.remoteCapabilities = capabilities
        self.hasRemoteActors    = []  # (localParent, remoteActor) addresses created remotely

        # The preRegOnly field indicates that this information is only
        # from a pre-registration.
        self.preRegOnly = preRegOnly

        # preRegistered is not None if the ConventionRegister has the
        # preRegister flag set.  This indicates a call from
        # preRegisterRemoteSystem.  The pingValid is only used for
        # preRegistered systems and is used to determine how long an
        # active check of the preRegistered remote is valid.  If
        # pingValid is expired, the local attempts to send a
        # QueryExists message (which will retry) and a QueryAck will
        # reset pingValid to another CONVENTION_REGISTRATION_PERIOD.
        # The pingPending is true while the QueryExists is pending and
        # will suppress additional pingPending messages.  A success or
        # failure completion of a QueryExists message will reset
        # pingPending to false.  Note that pinging occurs continually
        # for a preRegistered remote, regardless of whether or not its
        # Convention membership is currently valid.
        self.preRegistered      = None   # or PreRegistration object

        self._reset_valid_timer()

    def createdActor(self, localParentAddress, newActorAddress):
        entry = localParentAddress, newActorAddress
        if entry not in self.hasRemoteActors:
            self.hasRemoteActors.append(entry)

    def refresh(self, remoteCapabilities, preReg=False):
        self.remoteCapabilities = remoteCapabilities
        self._reset_valid_timer()
        self.preRegOnly = preReg
        if self.preRegistered:
            self.preRegistered.refresh()

    def _reset_valid_timer(self):
        # registryValid is a timer that is usually set to a multiple
        # of the convention re-registration period.  Each successful
        # convention re-registration resets the timer to the maximum
        # value (actually, it replaces this structure with a newly
        # generated structure).  If the timer expires, the remote is
        # declared as dead and the registration is removed (or
        # quiesced if it is a pre-registration).
        self.registryValid = ExpiryTime(CONVENTION_REREGISTRATION_PERIOD *
                                        CONVENTION_REGISTRATION_MISS_MAX)

    def __str__(self):
        return 'ActorSystem @ %s, registry valid for %s with %s' % (
            str(self.remoteAddress),
            str(self.registryValid),
            str(self.remoteCapabilities))


class HysteresisCancel(object):
    def __init__(self, cancel_addr):
        self.cancel_addr = cancel_addr


class HysteresisSend(TransmitIntent): pass


class LostRemote(object):
    # tells transport to reset (close sockets, drop buffers, etc.)
    def __init__(self, lost_addr):
        self.lost_addr = lost_addr


class LocalConventionState(object):
    def __init__(self, myAddress, capabilities, sCBStats,
                 getConventionAddressFunc):
        self._myAddress = myAddress
        self._capabilities = capabilities
        self._sCBStats = sCBStats
        self._conventionMembers = AssocList() # key=Remote Admin Addr, value=ConventionMemberData
        self._conventionNotificationHandlers = []
        self._getConventionAddr = getConventionAddressFunc
        self._conventionAddress = getConventionAddressFunc(capabilities)
        self._conventionRegistration = ExpiryTime(CONVENTION_REREGISTRATION_PERIOD)
        self._has_been_activated = False


    @property
    def myAddress(self):
        return self._myAddress

    @property
    def capabilities(self):
        return self._capabilities

    def updateStatusResponse(self, resp):
        resp.setConventionLeaderAddress(self.conventionLeaderAddr)
        resp.setConventionRegisterTime(self._conventionRegistration)
        for each in self._conventionMembers.values():
            resp.addConventioneer(each.remoteAddress, each.registryValid)
        resp.setNotifyHandlers(self._conventionNotificationHandlers)

    def active_in_convention(self):
        return self.conventionLeaderAddr and \
            not self._conventionMembers.find(self.conventionLeaderAddr)

    @property
    def conventionLeaderAddr(self):
        return self._conventionAddress

    def isConventionLeader(self):
        return self.conventionLeaderAddr == self.myAddress

    def capabilities_have_changed(self, new_capabilities):
        self._capabilities = new_capabilities
        return self.setup_convention()

    def setup_convention(self, activation=False):
        self._has_been_activated |= activation
        rmsgs = []
        self._conventionAddress = self._getConventionAddr(self.capabilities)
        leader_is_gone = (self._conventionMembers.find(self.conventionLeaderAddr) is None) if self.conventionLeaderAddr else True
        if not self.isConventionLeader() and self.conventionLeaderAddr:
            thesplog('Admin registering with Convention @ %s (%s)',
                     self.conventionLeaderAddr,
                     'first time' if leader_is_gone else 're-registering',
                     level=logging.INFO, primary=True)
            rmsgs.append(
                HysteresisSend(self.conventionLeaderAddr,
                               ConventionRegister(self.myAddress,
                                                  self.capabilities,
                                                  leader_is_gone),
                               onSuccess = self._setupConventionCBGood,
                               onError = self._setupConventionCBError))
            rmsgs.append(LogAggregator(self.conventionLeaderAddr))
        self._conventionRegistration = ExpiryTime(CONVENTION_REREGISTRATION_PERIOD)
        return rmsgs

    def _setupConventionCBGood(self, result, finishedIntent):
        self._sCBStats.inc('Admin Convention Registered')
        if hasattr(self, '_conventionLeaderMissCount'):
            delattr(self, '_conventionLeaderMissCount')

    def _setupConventionCBError(self, result, finishedIntent):
        self._sCBStats.inc('Admin Convention Registration Failed')
        if hasattr(self, '_conventionLeaderMissCount'):
            self._conventionLeaderMissCount += 1
        else:
            self._conventionLeaderMissCount = 1
        if self._conventionLeaderMissCount < CONVENTION_REGISTRATION_MISS_MAX:
            thesplog('Admin cannot register with convention @ %s (miss %d): %s',
                     finishedIntent.targetAddr,
                     self._conventionLeaderMissCount,
                     result, level=logging.WARNING, primary=True)
        else:
            thesplog('Admin convention registration lost @ %s (miss %d): %s',
                     finishedIntent.targetAddr,
                     self._conventionLeaderMissCount,
                     result, level=logging.ERROR, primary=True)

    def got_convention_invite(self, sender):
        self._conventionAddress = sender
        return self.setup_convention()

    def got_convention_register(self, regmsg):
        self._sCBStats.inc('Admin Handle Convention Registration')
        # Registrant may re-register if changing capabilities
        rmsgs = []
        registrant = regmsg.adminAddress
        prereg = getattr(regmsg, 'preRegister', False)  # getattr used; see definition
        existing = self._conventionMembers.find(registrant)
        thesplog('Got Convention %sregistration from %s (%s) (new? %s)',
                 'pre-' if prereg else '',
                 registrant,
                 'first time' if regmsg.firstTime else 're-registering',
                 not existing,
                 level=logging.INFO)
        if registrant == self.myAddress:
            # Either remote failed getting an external address and is
            # using 127.0.0.1 or else this is a malicious attempt to
            # make us talk to ourselves.  Ignore it.
            thesplog('Convention registration from %s is an invalid address; ignoring.',
                     registrant,
                     level=logging.WARNING)
            return rmsgs
        #if prereg or (regmsg.firstTime and not prereg):
        if regmsg.firstTime or (prereg and not existing):
            # erase knowledge of actors associated with potential
            # former instance of this system
            existing = False
            rmsgs.extend(self._remote_system_cleanup(registrant))
        if existing:
            existingPreReg = existing.preRegOnly
            existing.refresh(regmsg.capabilities, prereg)
            self._conventionMembers.add(registrant, existing)
        else:
            newmember = ConventionMemberData(registrant,
                                             regmsg.capabilities,
                                             prereg)
            if prereg:
                # Need to attempt registration immediately
                newmember.preRegistered = PreRegistration()
            self._conventionMembers.add(registrant, newmember)

        # Convention Members normally periodically initiate a
        # membership message, to which the leader confirms by
        # responding; if this was a pre-registration, that identifies
        # this system as the "leader" for that remote.  Also, if the
        # remote sent this because it was a pre-registration leader,
        # it doesn't yet have all the member information so the member
        # should respond.
        #if self.isConventionLeader() or prereg or regmsg.firstTime:
        if self.isConventionLeader() or prereg or regmsg.firstTime or \
           (existing and existing.preRegistered):
            if not existing or not existing.preRegOnly: # or prereg:
                first = prereg
                # If we are the Convention Leader, this would be the point to
                # inform all other registrants of the new registrant.  At
                # present, there is no reciprocity here, so just update the
                # new registrant with the leader's info.
                rmsgs.append(
                    TransmitIntent(registrant,
                                   ConventionRegister(self.myAddress,
                                                      self.capabilities,
                                                      first)))

        if (existing and existingPreReg and not existing.preRegOnly) or \
           (not existing and newmember and not prereg):
            rmsgs.extend(self._notifications_of(
                ActorSystemConventionUpdate(registrant,
                                            regmsg.capabilities,
                                            True)))
        return rmsgs

    def _notifications_of(self, msg):
        return [TransmitIntent(H, msg) for H in self._conventionNotificationHandlers]

    def add_notification_handler(self, addr):
        if addr not in self._conventionNotificationHandlers:
            self._conventionNotificationHandlers.append(addr)
            # Now update the registrant on the current state of all convention members
            return [TransmitIntent(addr,
                                   ActorSystemConventionUpdate(M.remoteAddress,
                                                               M.remoteCapabilities,
                                                               True))
                    for M in self._conventionMembers.values()]

    def remove_notification_handler(self, addr):
        self._conventionNotificationHandlers = [
            H for H in self._conventionNotificationHandlers
            if H != addr]

    def got_convention_deregister(self, deregmsg):
        self._sCBStats.inc('Admin Handle Convention De-registration')
        remoteAdmin = deregmsg.adminAddress
        if remoteAdmin == self.myAddress:
            # Either remote failed getting an external address and is
            # using 127.0.0.1 or else this is a malicious attempt to
            # make us talk to ourselves.  Ignore it.
            thesplog('Convention deregistration from %s is an invalid address; ignoring.',
                     remoteAdmin,
                     level=logging.WARNING)
        if getattr(deregmsg, 'preRegistered', False): # see definition for getattr use
            existing = self._conventionMembers.find(remoteAdmin)
            if existing:
                existing.preRegistered = None
        return self._remote_system_cleanup(remoteAdmin)

    def got_system_shutdown(self):
        gen_ops = lambda addr: [HysteresisCancel(addr),
                                TransmitIntent(addr,
                                               ConventionDeRegister(self.myAddress)),
        ]
        if self.conventionLeaderAddr and \
           self.conventionLeaderAddr != self.myAddress:
            thesplog('Admin de-registering with Convention @ %s',
                     str(self.conventionLeaderAddr),
                     level=logging.INFO, primary=True)
            return gen_ops(self.conventionLeaderAddr)
        return join(fmap(gen_ops,
                         [M.remoteAddress
                          for M in self._conventionMembers.values()
                          if M.remoteAddress != self.myAddress]))

    def check_convention(self):
        rmsgs = []
        if not self._has_been_activated:
            return rmsgs
        if self.isConventionLeader() or not self.conventionLeaderAddr:
            missing = []
            for each in self._conventionMembers.values():
                if each.registryValid.expired():
                    missing.append(each)
            for each in missing:
                thesplog('%s missed %d checkins (%s); assuming it has died',
                         str(each),
                         CONVENTION_REGISTRATION_MISS_MAX,
                         str(each.registryValid),
                         level=logging.WARNING, primary=True)
                rmsgs.extend(self._remote_system_cleanup(each.remoteAddress))
            self._conventionRegistration = ExpiryTime(CONVENTION_REREGISTRATION_PERIOD)
        else:
            # Re-register with the Convention if it's time
            if self.conventionLeaderAddr and self._conventionRegistration.expired():
                rmsgs.extend(self.setup_convention())

        for member in self._conventionMembers.values():
            if member.preRegistered and \
               member.preRegistered.pingValid.expired() and \
               not member.preRegistered.pingPending:
                member.preRegistered.pingPending = True
                member.preRegistered.pingValid = ExpiryTime(CONVENTION_RESTART_PERIOD
                                                            if member.registryValid.expired()
                                                            else CONVENTION_REREGISTRATION_PERIOD)
                rmsgs.append(HysteresisSend(
                    member.remoteAddress, ConventionInvite(),
                    onSuccess = self._preRegQueryNotPending,
                    onError = self._preRegQueryNotPending))
        return rmsgs


    def _preRegQueryNotPending(self, result, finishedIntent):
        remoteAddr = finishedIntent.targetAddr
        member = self._conventionMembers.find(remoteAddr)
        if member and member.preRegistered:
            member.preRegistered.pingPending = False

    def _remote_system_cleanup(self, registrant):
        """Called when a RemoteActorSystem has exited and all associated
           Actors should be marked as exited and the ActorSystem
           removed from Convention membership.  This is also called on
           a First Time connection from the remote to discard any
           previous connection information.

        """
        thesplog('Convention cleanup or deregistration for %s (known? %s)',
                 registrant,
                 bool(self._conventionMembers.find(registrant)),
                 level=logging.INFO)
        rmsgs = [LostRemote(registrant)]
        cmr = self._conventionMembers.find(registrant)
        if cmr:

            # Send exited notification to conventionNotificationHandler (if any)
            for each in self._conventionNotificationHandlers:
                rmsgs.append(
                    TransmitIntent(each,
                                   ActorSystemConventionUpdate(cmr.remoteAddress,
                                                               cmr.remoteCapabilities,
                                                               False)))  # errors ignored

            # If the remote ActorSystem shutdown gracefully (i.e. sent
            # a Convention Deregistration) then it should not be
            # necessary to shutdown remote Actors (or notify of their
            # shutdown) because the remote ActorSystem should already
            # have caused this to occur.  However, it won't hurt, and
            # it's necessary if the remote ActorSystem did not exit
            # gracefully.

            for lpa, raa in cmr.hasRemoteActors:
                # ignore errors:
                rmsgs.append(TransmitIntent(lpa, ChildActorExited(raa)))
                # n.b. at present, this means that the parent might
                # get duplicate notifications of ChildActorExited; it
                # is expected that Actors can handle this.

            # Remove remote system from conventionMembers
            if not cmr.preRegistered:
                self._conventionMembers.rmv(registrant)
            else:
                # This conventionMember needs to stay because the
                # current system needs to continue issuing
                # registration pings.  By setting the registryValid
                # expiration to forever, this member won't re-time-out
                # and will therefore be otherwise ignored... until it
                # registers again at which point the membership will
                # be updated with new settings.
                cmr.registryValid = ExpiryTime(None)

        return rmsgs + [HysteresisCancel(registrant)]

    def sentByRemoteAdmin(self, envelope):
        for each in self._conventionMembers.values():
            if envelope.sender == each.remoteAddress:
                return True
        return False

    def convention_inattention_delay(self):
        return self._conventionRegistration or \
            ExpiryTime(CONVENTION_REREGISTRATION_PERIOD
                       if self.active_in_convention() or
                       self.isConventionLeader() else
                       CONVENTION_RESTART_PERIOD)

    def forward_pending_to_remote_system(self, childClass, envelope, sourceHash, acceptsCaps):
        alreadyTried = getattr(envelope.message, 'alreadyTried', [])

        remoteCandidates = [
            K
            for K in self._conventionMembers.values()
            if not K.registryValid.expired()
            and K.remoteAddress != envelope.sender # source Admin
            and K.remoteAddress not in alreadyTried
            and acceptsCaps(K.remoteCapabilities)]

        if not remoteCandidates:
            if self.isConventionLeader() or not self.conventionLeaderAddr:
                raise NoCompatibleSystemForActor(
                    childClass,
                    'No known ActorSystems can handle a %s for %s',
                    childClass, envelope.message.forActor)
            # Let the Convention Leader try to find an appropriate ActorSystem
            bestC = self.conventionLeaderAddr
        else:
            # distribute equally amongst candidates
            C = [(K.remoteAddress, len(K.hasRemoteActors))
                 for K in remoteCandidates]
            bestC = foldl(lambda best,possible:
                          best if best[1] <= possible[1] else possible,
                          C)[0]
            thesplog('Requesting creation of %s%s on remote admin %s',
                     envelope.message.actorClassName,
                     ' (%s)'%sourceHash if sourceHash else '',
                     bestC)
        if bestC not in alreadyTried:
            # Don't send request to this remote again, it has already
            # been tried.  This would also be indicated by that system
            # performing the add of self.myAddress as below, but if
            # there is disagreement between the local and remote
            # addresses, this addition will prevent continual
            # bounceback.
            alreadyTried.append(bestC)
        if self.myAddress not in alreadyTried:
            # Don't send request back to this actor system: it cannot
            # handle it
            alreadyTried.append(self.myAddress)
        envelope.message.alreadyTried = alreadyTried
        return [TransmitIntent(bestC, envelope.message)]


    def send_to_all_members(self, message, exception_list=None):
        return [HysteresisSend(M.remoteAddress, message)
                for M in self._conventionMembers.values()
                if M.remoteAddress not in (exception_list or [])]



class ConventioneerAdmin(GlobalNamesAdmin):
    """Extends the AdminCore+GlobalNamesAdmin with ActorSystem Convention
       functionality to support multi-host configurations.
    """
    def __init__(self, *args, **kw):
        super(ConventioneerAdmin, self).__init__(*args, **kw)
        self._cstate = LocalConventionState(
            self.myAddress,
            self.capabilities,
            self._sCBStats,
            getattr(self.transport, 'getConventionAddress', lambda c: None))
        self._pendingSources = {}  # key = sourceHash, value is array of PendingActor requests
        self._hysteresisSender = HysteresisDelaySender(self._send_intent)

    def _updateStatusResponse(self, resp):
        self._cstate.updateStatusResponse(resp)
        super(ConventioneerAdmin, self)._updateStatusResponse(resp)


    def _activate(self):
        # Called internally when this ActorSystem has been initialized
        # and should be activated for operations.
        if self.isShuttingDown(): return
        self._performIO(self._cstate.setup_convention(True))

    def h_ConventionInvite(self, envelope):
        if self.isShuttingDown(): return
        #self._performIO(self._cstate.got_convention_invite(envelope.sender))

    def h_ConventionRegister(self, envelope):
        if self.isShuttingDown(): return
        self._performIO(self._cstate.got_convention_register(envelope.message))


    def h_ConventionDeRegister(self, envelope):
        self._performIO(self._cstate.got_convention_deregister(envelope.message))

    def h_SystemShutdown(self, envelope):
        self._performIO(self._cstate.got_system_shutdown())
        return super(ConventioneerAdmin, self).h_SystemShutdown(envelope)

    def _performIO(self, iolist):
        for msg in iolist:
            if isinstance(msg, HysteresisCancel):
                self._hysteresisSender.cancelSends(msg.cancel_addr)
            elif isinstance(msg, HysteresisSend):
                #self._send_intent(msg)
                self._hysteresisSender.sendWithHysteresis(msg)
            elif isinstance(msg, LogAggregator):
                if getattr(self, 'asLogger', None):
                    thesplog('Setting log aggregator of %s to %s', self.asLogger, msg)
                    self._send_intent(TransmitIntent(self.asLogger, msg))
            elif isinstance(msg, LostRemote):
                if hasattr(self.transport, 'lostRemote'):
                    self.transport.lostRemote(msg.lost_addr)
            else:
                self._send_intent(msg)

    def run(self):
        # Main loop for convention management.  Wraps the lower-level
        # transport with a stop at the next needed convention
        # registration period to re-register.
        try:
            while not getattr(self, 'shutdown_completed', False):
                delay = min(self._cstate.convention_inattention_delay(),
                            ExpiryTime(None) if self._hysteresisSender.delay.expired() else
                            self._hysteresisSender.delay
                )
                # n.b. delay does not account for soon-to-expire
                # pingValids, but since delay will not be longer than
                # a CONVENTION_REREGISTRATION_PERIOD, the worst case
                # is a doubling of a pingValid period (which should be fine).
                r = self.transport.run(self.handleIncoming, delay.remaining())

                # Check Convention status based on the elapsed time
                self._performIO(self._cstate.check_convention())

                self._hysteresisSender.checkSends()
        except Exception as ex:
            import traceback
            thesplog('ActorAdmin uncaught exception: %s', traceback.format_exc(),
                     level=logging.ERROR, exc_info=True)
        thesplog('Admin time to die', level=logging.DEBUG)


    # ---- Source Hash Transfers --------------------------------------------------

    def h_SourceHashTransferRequest(self, envelope):
        sourceHash = envelope.message.sourceHash
        src = self._sources.get(sourceHash, None)
        self._send_intent(
            TransmitIntent(envelope.sender,
                           SourceHashTransferReply(sourceHash,
                                                   src and src.zipsrc,
                                                   src and src.srcInfo)))
        return True


    def h_SourceHashTransferReply(self, envelope):
        sourceHash = envelope.message.sourceHash
        pending = self._pendingSources[sourceHash]
        del self._pendingSources[sourceHash]
        if envelope.message.isValid():
            self._sources[sourceHash] = ValidSource(sourceHash,
                                                    envelope.message.sourceData,
                                                    getattr(envelope.message,
                                                            'sourceInfo', None))
            for each in pending:
                self.h_PendingActor(each)
        else:
            for each in pending:
                self._sendPendingActorResponse(each, None,
                                               errorCode = PendingActorResponse.ERROR_Invalid_SourceHash)
        return True


    def h_ValidateSource(self, envelope):
        if not envelope.message.sourceData and \
           envelope.sender != self._cstate.conventionLeaderAddr:
            # Propagate source unload requests to all convention members
            self._performIO(
                self._cstate.send_to_all_members(
                    envelope.message,
                    # Do not propagate if this is where the
                    # notification came from; prevents indefinite
                    # bouncing of this message as long as the
                    # convention structure is a DAG.
                    [envelope.sender]))
        super(ConventioneerAdmin, self).h_ValidateSource(envelope)
        return False  # might have sent with hysteresis, so break out to local _run


    def _acceptsRemoteLoadedSourcesFrom(self, pendingActorEnvelope):
        allowed = self.capabilities.get('AllowRemoteActorSources', 'yes')
        return allowed.lower() == 'yes' or \
            (allowed == 'LeaderOnly' and
             pendingActorEnvelope.sender == self.conventionLeaderAddr)


    # ---- Remote Actor interactions ----------------------------------------------


    def h_PendingActor(self, envelope):
        sourceHash = envelope.message.sourceHash
        childRequirements = envelope.message.targetActorReq
        thesplog('Pending Actor request received for %s%s reqs %s from %s',
                 envelope.message.actorClassName,
                 ' (%s)'%sourceHash if sourceHash else '',
                 childRequirements, envelope.sender)
        # If this request was forwarded by a remote Admin and the
        # sourceHash is not known locally, request it from the sending
        # remote Admin
        if sourceHash and \
           sourceHash not in self._sources and \
           self._cstate.sentByRemoteAdmin(envelope) and \
           self._acceptsRemoteLoadedSourcesFrom(envelope):
            requestedAlready = self._pendingSources.get(sourceHash, False)
            self._pendingSources.setdefault(sourceHash, []).append(envelope)
            if not requestedAlready:
                self._hysteresisSender.sendWithHysteresis(
                    TransmitIntent(envelope.sender,
                                   SourceHashTransferRequest(sourceHash)))
                return False  # sent with hysteresis, so break out to local _run
            return True
        # If the requested ActorClass is compatible with this
        # ActorSystem, attempt to start it, otherwise forward the
        # request to any known compatible ActorSystem.
        childClass = envelope.message.actorClassName
        try:
            childClass = actualActorClass(envelope.message.actorClassName,
                                          partial(loadModuleFromHashSource,
                                                  sourceHash,
                                                  self._sources)
                                          if sourceHash else None)
            acceptsCaps = lambda caps: checkActorCapabilities(childClass, caps,
                                                              childRequirements)
            if not acceptsCaps(self.capabilities):
                if envelope.message.forActor is None:
                    # Request from external; use sender address
                    envelope.message.forActor = envelope.sender
                iolist = self._cstate.forward_pending_to_remote_system(
                    childClass, envelope, sourceHash, acceptsCaps)
                for each in iolist:
                    # Expected to be only one; if the transmit fails,
                    # route it back here so that the next possible
                    # remote can be tried.
                    each.addCallback(onFailure=self._pending_send_failed)
                self._performIO(iolist)
                return True
        except NoCompatibleSystemForActor as ex:
            thesplog(str(ex), level=logging.WARNING, primary=True)
            self._sendPendingActorResponse(
                envelope, None,
                errorCode=PendingActorResponse.ERROR_No_Compatible_ActorSystem)
            return True
        except InvalidActorSourceHash:
            self._sendPendingActorResponse(
                envelope, None,
                errorCode=PendingActorResponse.ERROR_Invalid_SourceHash)
            return True
        except InvalidActorSpecification:
            self._sendPendingActorResponse(
                envelope, None,
                errorCode=PendingActorResponse.ERROR_Invalid_ActorClass)
            return True
        except ImportError as ex:
            self._sendPendingActorResponse(
                envelope, None,
                errorCode=PendingActorResponse.ERROR_Import,
                errorStr=str(ex))
            return True
        except AttributeError as ex:
            # Usually when the module has no attribute FooActor
            self._sendPendingActorResponse(
                envelope, None,
                errorCode=PendingActorResponse.ERROR_Invalid_ActorClass,
                errorStr=str(ex))
            return True
        except Exception as ex:
            import traceback
            thesplog('Exception "%s" handling PendingActor: %s', ex, traceback.format_exc(), level=logging.ERROR)
            self._sendPendingActorResponse(
                envelope, None,
                errorCode=PendingActorResponse.ERROR_Invalid_ActorClass,
                errorStr=str(ex))
            return True
        return super(ConventioneerAdmin, self).h_PendingActor(envelope)


    def _pending_send_failed(self, result, intent):
        self.h_PendingActor(ReceiveEnvelope(msg=intent.message, sender=self.myAddress))


    def h_NotifyOnSystemRegistration(self, envelope):
        if envelope.message.enableNotification:
            self._performIO(
                self._cstate.add_notification_handler(envelope.sender))
        else:
            self._cstate.remove_notification_handler(envelope.sender)
        return True


    def h_PoisonMessage(self, envelope):
        self._cstate.remove_notification_handler(envelope.sender)


    def _handleChildExited(self, childAddr):
        self._cstate.remove_notification_handler(childAddr)
        return super(ConventioneerAdmin, self)._handleChildExited(childAddr)


    def h_CapabilityUpdate(self, envelope):
        msg = envelope.message
        updateLocals = self._updSystemCapabilities(msg.capabilityName,
                                                   msg.capabilityValue)
        if not self.isShuttingDown():
            self._performIO(
                self._cstate.capabilities_have_changed(self.capabilities))
        if updateLocals:
            self._capUpdateLocalActors()
        return False  # might have sent with Hysteresis, so return to _run loop here
