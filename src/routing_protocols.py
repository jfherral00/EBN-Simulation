from netsquid.protocols import LocalProtocol, Signals
import netsquid as ns
from netsquid.qubits import ketstates as ks
from icecream import ic
from netsquid.components.instructions import INSTR_MEASURE_BELL, INSTR_MEASURE, INSTR_X, INSTR_Z, INSTR_SWAP, INSTR_CNOT, IGate
from netsquid.util.simtools import sim_time
from netsquid.qubits import qubitapi as qapi
from pydynaa import EventExpression, EventType
from protocols import RouteProtocol

class LinkFidelityProtocol(LocalProtocol):
    '''
    TODO: Document constructor properties
    '''
            
    def __init__(self, networkmanager, origin, dest, link, qsource_index, num_runs=100, name=None):
        self._origin = origin
        self._dest = dest
        self._link = link
        self._num_runs = num_runs
        self._networkmanager = networkmanager
        self._qsource_index = qsource_index
        name = name if name else f"LinkFidelityEstimator_{origin.name}_{dest.name}"
        super().__init__(nodes={"A": origin, "B": dest}, name=name)

        #Get memory positions in left node and right node.
        self._memory_left = networkmanager.get_mem_position(self._origin.name, self._link, self._qsource_index)
        self._memory_right = networkmanager.get_mem_position(self._dest.name, self._link, self._qsource_index)
        self._portleft = self._origin.qmemory.ports[f"qin{self._memory_left}"]
        self._portright = self._dest.qmemory.ports[f"qin{self._memory_right}"]

        self.fidelities = []
        
        #Calculate time to wait until we decide the qubit is lost.
        # qsource_elay + transmission time
        self._delay = 0
        #transmission delay
        self._delay += 1e9 * float(networkmanager.get_config('links',link,'distance'))/float(networkmanager.get_config('links',link,'photon_speed_fibre'))
        #qsource delay
        emission_delay = float(networkmanager.get_config('links',link,'source_delay')) \
                if networkmanager.get_config('links',link,'source_delay') != 'NOT_FOUND' else 0
        self._delay += emission_delay
        # We need to ad a little delay so that no false timeouts appear
        self._delay += 100
        
        #TODO: Delete these lines if time calculation is correct
        #To make sure we measure, we set 4 times the expected value
        #self._delay = 4 * 1e9 * float(networkmanager.get_config('links',link,'distance'))/float(networkmanager.get_config('links',link,'photon_speed_fibre'))

    def run(self):
        #Signal Qsource to start. Must trigger correct source
        trig_origin = self._origin if self._networkmanager.get_config('nodes',self._origin.name,'type') == 'switch' else self._dest
        trig_origin.subcomponents[f"qsource_{trig_origin.name}_{self._link}_0"].trigger()

        #Lost qubit signal
        evtypetimer = EventType("Timer","Qubit is lost")
        evexpr_timer = EventExpression(source=self, event_type=evtypetimer)

        #Get type of EPR to use
        epr_state = ks.b00 if self._networkmanager.get_config('epr_pair','epr_pair') == 'PHI_PLUS' else ks.b01

        for i in range(self._num_runs):
            #Create timer in order to detect lost qubit
            timer_event = self._schedule_after(self._delay, evtypetimer)
            #Wait for qubits to arrive at both ends or detect a lost qubit
            evexpr = yield evexpr_timer | (self.await_port_input(self._portleft) & self.await_port_input(self._portright))
            
            if evexpr.second_term.value: #there are qubits in both ends
                #Unschedule lost qubit timer
                timer_event.unschedule()
                #Measure fidelity and add it to the list
                qubit_a, = self._origin.qmemory.peek([self._memory_left])
                qubit_b, = self._dest.qmemory.peek([self._memory_right])
                self.fidelities.append(ns.qubits.fidelity([qubit_a, qubit_b], epr_state, squared=True))
            else:
                #qubit is lost, we set a fidelity of 0
                #We set a value different from 0 to avoid later log of 0
                self.fidelities.append(1e-99)
            
            #trigger new fidelity measurement
            trig_origin.subcomponents[f"qsource_{trig_origin.name}_{self._link}_0"].trigger()

class PathFidelityProtocol(LocalProtocol):

    def __init__(self, networkmanager, path, num_runs, purif_rounds= 0, name=None):
        self._purif_rounds = purif_rounds
        self._num_runs = num_runs
        self._path = path
        self._networkmanager = networkmanager

        name = name if name else f"PathFidelityEstimator_{path['request']}"
        super().__init__(nodes=networkmanager.network.nodes, name=name)

        self._ent_request = 'START_ENTANGLEMENT'
        self.add_signal(self._ent_request)
        
        ent_start_expression = self.await_signal(self, self._ent_request)
        self.add_subprotocol(RouteProtocol(networkmanager,path,ent_start_expression,0))

    def set_purif_rounds(self, purif_rounds):
        self._purif_rounds = purif_rounds
        subproto = self.subprotocols[f"RouteProtocol_{self._path['request']}"]
        subproto.set_purif_rounds(purif_rounds)

    def run(self):
        self.start_subprotocols()

        #Get type of EPR to use
        epr_state = ks.b00 if self._networkmanager.get_config('epr_pair','epr_pair') == 'PHI_PLUS' else ks.b01

        #Though in this simulations positions in nodes are always 0, we query in case this is changed in the future
        first_link = self._path['comms'][0]['links'][0]
        last_link = self._path['comms'][-1]['links'][0]
        mem_posA_1 = self._networkmanager.get_mem_position(self._path['nodes'][0],first_link.split('-')[0],first_link.split('-')[1])
        mem_posB_1 = self._networkmanager.get_mem_position(self._path['nodes'][-1],last_link.split('-')[0],last_link.split('-')[1])

        for i in range(self._num_runs):
            start_time = sim_time()

            #Send signal for entanglement generation
            self.send_signal(self._ent_request)

            #Wait for  entanglement to be generated on both ends
            yield self.await_signal(self.subprotocols[f"RouteProtocol_{self._path['request']}"],Signals.SUCCESS)

            #Measure fidelity and send metrics to datacollector
            qa, = self._networkmanager.network.get_node(self._path['nodes'][0]).qmemory.pop(positions=[mem_posA_1])
            qb, = self._networkmanager.network.get_node(self._path['nodes'][-1]).qmemory.pop(positions=[mem_posB_1])
            fid = qapi.fidelity([qa, qb], epr_state, squared=True)
            result = {
                'posA': mem_posA_1,
                'posB': mem_posB_1,
                'Fidelity': fid,
                'time': sim_time() - start_time
            }
            #send result to datacollector
            self.send_signal(Signals.SUCCESS, result)
            
