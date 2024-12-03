import numpy as np
from icecream import ic
from random import uniform, gauss
from netsquid.qubits import ketstates as ks
from netsquid.components import Message, QuantumProcessor, QuantumProgram, PhysicalInstruction, ClassicalChannel
from netsquid.components.models.qerrormodels import DepolarNoiseModel, DephaseNoiseModel, QuantumErrorModel
from netsquid.components.instructions import INSTR_MEASURE_BELL, INSTR_X, INSTR_Z, INSTR_I, INSTR_SWAP
from netsquid.nodes import Node
from netsquid.protocols import LocalProtocol, NodeProtocol, Signals
from netsquid.util import simlog
from netsquid.components.component import Message, Port
import netsquid.qubits.operators as ops
from pydynaa import EventExpression
from netsquid.components.instructions import INSTR_MEASURE_BELL, INSTR_MEASURE, INSTR_X, INSTR_Z, INSTR_SWAP, INSTR_CNOT, IGate
from pydynaa import EventExpression, EventType
from netsquid.util.simtools import sim_time

__all__ = [
    "SwapProtocol",
    "SwapCorrectProgram",
    "CorrectProtocol",
    'DistilProtocol',
    "RouteProtocol"
]

class RouteProtocol(LocalProtocol):
    '''
    Class that implements the protocol responsible for generating an EPR between source
    and destination in the path. Will wail for a request and coordinate the different network protocols
    in order to make available an EPR en memories of origin and destination
    Parameters:
        - networkmanager: instance of the network manager that stores the network
        - path: calculated path for servicing the request
        - start_expression: event expression that will trigger the EPR generation
        - phase: 'routing' or 'application'
        - purif_rounds: number of needed purification rounds
        - name: name of the protocol
    '''

    def __init__(self, networkmanager, path, start_expression, phase = 'routing', purif_rounds= 0, name=None):
        self._path = path
        self._networkmanager = networkmanager
        self.start_expression = start_expression
        self._purif_rounds = purif_rounds
        name = name if name else f"RouteProtocol_{path['request']}"
        super().__init__(nodes=networkmanager.network.nodes, name=name)
        first_link = self._path['comms'][0]['links'][0]
        last_link = self._path['comms'][-1]['links'][0]
        self._mem_posA_1 = self._networkmanager.get_mem_position(self._path['nodes'][0],first_link.split('-')[0],first_link.split('-')[1])
        self._mem_posB_1 = self._networkmanager.get_mem_position(self._path['nodes'][-1],last_link.split('-')[0],last_link.split('-')[1])
        self._portleft_1 = self._networkmanager.network.get_node(self._path['nodes'][0]).qmemory.ports[f"qin{self._mem_posA_1}"]

        # add purification signals
        #start purification signal
        self._start_purif_signal = 'START_PURIFICATION'
        self.add_signal(self._start_purif_signal)
        #end purification signal: Distil protocol will send 0 if purification successful or 1 if failed
        self._purif_result_signal = 'PURIF_DONE'
        self.add_signal(self._purif_result_signal)
        #add correct protocol restart signal. Needed when purification is used and one quit is lost
        self._restart_signal = 'RESTART_CORRECT_PROTOCOL'
        self.add_signal(self._restart_signal)

        # preparation of entanglement swaping from second to the last-1
        for nodepos in range(1,len(path['nodes'])-1):
            node = path['nodes'][nodepos]
            link_left = path['comms'][nodepos-1]['links'][0]
            link_right = path['comms'][nodepos]['links'][0]
            mem_pos_left = networkmanager.get_mem_position(node,link_left.split('-')[0],link_left.split('-')[1])
            mem_pos_right = networkmanager.get_mem_position(node,link_right.split('-')[0],link_right.split('-')[1])
            subprotocol = SwapProtocol(node=networkmanager.network.get_node(node), mem_left=mem_pos_left, mem_right=mem_pos_right, name=f"SwapProtocol_{node}_{path['request']}_1", request = path['request'])
            self.add_subprotocol(subprotocol)

        # preparation of correct protocol in final node
        epr_state =  self._networkmanager.get_config('epr_pair','epr_pair')
        mempos= networkmanager.get_mem_position(path['nodes'][-1],last_link.split('-')[0],last_link.split('-')[1])
        restart_expr = self.await_signal(self,self._restart_signal)
        subprotocol = CorrectProtocol(networkmanager.network.get_node(path['nodes'][-1]), mempos, len(path['nodes']), f"CorrectProtocol_{path['request']}_1", path['request'],restart_expr, epr_state)
        self.add_subprotocol(subprotocol)

        if purif_rounds > 0:
            #If protocol is being instanced with purification from the beggining we need to add second link protocols
            self._init_second_link_protocols('distil')

        # calculate total distance and delay, in order to set timer to detect lost qubit
        self._total_delay = 0
        max_source_delay = 0
        for comm in path['comms']:
            link_name = comm['links'][0].split('-')[0]
            #Add time corresponding to transmission
            distance = float(networkmanager.get_config('links',link_name,'distance'))
            photon_speed = float(networkmanager.get_config('links',link_name,'photon_speed_fibre'))
            self._total_delay += 1e9 * distance / photon_speed
            #Add time corresponding to qsource emission
            emission_delay = float(networkmanager.get_config('links',link_name,'source_delay')) \
                if networkmanager.get_config('links',link_name,'source_delay') != 'NOT_FOUND' else 0
            if emission_delay > max_source_delay:
                max_source_delay = emission_delay
        self._total_delay += max_source_delay
        
        #We need to add some nanoseconds to timer, to discard false timeout positives
        # when tomeout and correct transmission matches. If distances are short we 
        # can receive a lost qubit signal when it is not correct
        self._total_delay += 100
        
        #We should add time corresponging to Bell measurements in switches and X/Z in end node
        max_swap_time = 0
        correction_time = 0
        for node in path['nodes'][1:]:
            if networkmanager.get_config('nodes',node,'type') == 'switch':
                gate_duration = networkmanager.get_config('nodes',node,'gate_duration') \
                    if networkmanager.get_config('nodes',node,'gate_duration') != 'NOT_FOUND' else 0
                gate_duration_CX = networkmanager.get_config('nodes',node,'gate_duration_CX') \
                    if networkmanager.get_config('nodes',node,'gate_duration_CX') != 'NOT_FOUND' else gate_duration
                measurements_duration = networkmanager.get_config('nodes',node,'measurements_duration') \
                    if networkmanager.get_config('nodes',node,'measurements_duration') != 'NOT_FOUND' else gate_duration
                if gate_duration + gate_duration_CX + measurements_duration > max_swap_time:
                    max_swap_time = gate_duration + gate_duration_CX + measurements_duration
            else:
                num_switches = len(path['nodes']) - 2
                gate_duration = networkmanager.get_config('nodes',node,'gate_duration') \
                    if networkmanager.get_config('nodes',node,'gate_duration') != 'NOT_FOUND' else 0
                #Worse case: X and Z corrections to apply
                correction_time = 2 * gate_duration
                
        self._total_delay += max_swap_time + correction_time 
 
        #When several requests are processed, we should also add time related to Bell measurements for those requests
        if phase == 'application':
            #Add 3% as margin for possible delays
            self._total_delay += (len(networkmanager.get_paths()) -1) * (gate_duration + gate_duration_CX + measurements_duration) *1.03

    def signal_sources(self,index=[1]):
        '''
        Signals all sources in the path in order to generate EPR
        Receives the index to trigger the generation. If none, only first instance will be triggered
        If index=[1,2] then both instances are signaled (purification)
        '''
        if index not in [[1],[2],[1,2]]:
            raise ValueError('Unsupported trigger generation')
        for link in self._path['comms']:
            trigger_node = self._networkmanager.network.get_node(link['source'])
            for i in index:
                trigger_link = link['links'][i-1].split('-')[0]
                trigger_link_index = link['links'][i-1].split('-')[1]
                trigger_node.subcomponents[f"qsource_{trigger_node.name}_{trigger_link}_{trigger_link_index}"].trigger()

    def set_purif_rounds(self, purif_rounds):
        self._purif_rounds = purif_rounds
        if self._purif_rounds == 1: # Set memories for the second link
            self._init_second_link_protocols('distil')
            #Update delay time with purification operations in order to detect lost qubit
            node_name = self._path['nodes'][-1]
            gate_duration_rotations = self._networkmanager.get_config('nodes',node_name,'gate_duration_rotations') \
                if self._networkmanager.get_config('nodes',node_name,'gate_duration_rotations') != 'NOT_FOUND' else 0
            gate_duration_CX = self._networkmanager.get_config('nodes',node_name,'gate_duration_CX') \
                    if self._networkmanager.get_config('nodes',node_name,'gate_duration_CX') != 'NOT_FOUND' else 0        
            measurements_duration = self._networkmanager.get_config('nodes',node_name,'measurements_duration') \
                    if self._networkmanager.get_config('nodes',node_name,'measurements_duration') != 'NOT_FOUND' else 0        
            self._total_delay += 2 * gate_duration_rotations + gate_duration_CX + measurements_duration
            

    def _init_second_link_protocols(self, purif_proto):
        '''
        Initialices memory positions for the second index of the links and
        creates protocols for this second instance of the link
        Receives purification protocol to use. Right now only distil
        '''        
        first_link = self._path['comms'][0]['links'][1]
        last_link = self._path['comms'][-1]['links'][1]
        self._mem_posA_2 = self._networkmanager.get_mem_position(self._path['nodes'][0],first_link.split('-')[0],first_link.split('-')[1])
        self._mem_posB_2 = self._networkmanager.get_mem_position(self._path['nodes'][-1],last_link.split('-')[0],last_link.split('-')[1])
        self._portleft_2 = self._networkmanager.network.get_node(self._path['nodes'][0]).qmemory.ports[f"qin{self._mem_posA_2}"]

        #add SwapProtocol in second instance of link
        for nodepos in range(1,len(self._path['nodes'])-1):
            node = self._path['nodes'][nodepos]
            link_left = self._path['comms'][nodepos-1]['links'][1]
            link_right = self._path['comms'][nodepos]['links'][1]
            mem_pos_left = self._networkmanager.get_mem_position(node,link_left.split('-')[0],link_left.split('-')[1])
            mem_pos_right = self._networkmanager.get_mem_position(node,link_right.split('-')[0],link_right.split('-')[1])
            subprotocol = SwapProtocol(node=self._networkmanager.network.get_node(node), mem_left=mem_pos_left, mem_right=mem_pos_right, name=f"SwapProtocol_{node}_{self._path['request']}_2", request = self._path['request'])
            self.add_subprotocol(subprotocol)

        #add Correction protocol for second instance of link
        epr_state = epr_state =  self._networkmanager.get_config('epr_pair','epr_pair')
        mempos = self._networkmanager.get_mem_position(self._path['nodes'][-1],last_link.split('-')[0],last_link.split('-')[1])
        restart_expr = self.await_signal(self,self._restart_signal)
        subprotocol = CorrectProtocol(self._networkmanager.network.get_node(self._path['nodes'][-1]), mempos, len(self._path['nodes']), f"CorrectProtocol_{self._path['request']}_2", self._path['request'],restart_expr, epr_state)
        self.add_subprotocol(subprotocol)

        #add purification protocol
        if purif_proto not in ['distil']:
            raise ValueError(f"{purif_proto} is a not implemented purification protocol")
       
        nodeA = self._networkmanager.network.get_node(self._path['nodes'][0])
        nodeB = self._networkmanager.network.get_node(self._path['nodes'][-1])
 
        #Distil will wait for START_PURIFICATION signal
        #start_expression = self.await_signal(self, Signals.WAITING)
        start_expression = self.await_signal(self, self._start_purif_signal)
        if purif_proto == 'distil':
            self.add_subprotocol(DistilProtocol(nodeA, nodeA.ports[f"ccon_distil_{nodeA.name}_{self._path['request']}"],
            'A',self._mem_posA_1,self._mem_posA_2,start_expression, msg_header='distil', name=f"DistilProtocol_{nodeA.name}_{self._path['request']}"))
            self.add_subprotocol(DistilProtocol(nodeB, nodeB.ports[f"ccon_distil_{nodeB.name}_{self._path['request']}"],
            'B',self._mem_posB_1,self._mem_posB_2,start_expression, msg_header='distil',name=f"DistilProtocol_{nodeB.name}_{self._path['request']}"))

    def run(self):
        self.start_subprotocols()
        #Qubit lost when qchannel model has losses
        evtypetimer = EventType("Timer","Qubit is lost")
        #set event type in order to detect qubit losses
        evexpr_timer = EventExpression(source=self, event_type=evtypetimer)

        #for i in range(self._num_runs):
        while True:
            #Wait for an entanglement request
            yield self.start_expression

            round_done = False
            start_time = sim_time()
            while not round_done: #need to repeat in case qubit is lost
                if self._purif_rounds == 0:
                    #trigger all sources in the path
                    self.signal_sources(index=[1])

                    timer_event = self._schedule_after(self._total_delay, evtypetimer)

                    evexpr_protocol = (self.await_port_input(self._portleft_1)) & \
                        (self.await_signal(self.subprotocols[f"CorrectProtocol_{self._path['request']}_1"], Signals.SUCCESS))
                    #if timer is triggered, qubit has been lost in a link. Else entanglement
                    # swapping has succeeded

                    evexpr = yield evexpr_timer | evexpr_protocol
                    
                    if evexpr.second_term.value: #swapping ok
                        timer_event.unschedule()
                        round_done = True
                    else:
                        #qubit is lost, must restart
                        #ic(f"{self.name} Lost qubit in Route protocol")
                        #restart correction protocol
                        self.send_signal(self._restart_signal)
                        #repeat round
                        continue

                else: #we have to perform purification
                    purification_done = False
                    while not purification_done:
                        pur_round = 0
                        while (pur_round <= self._purif_rounds):# and (qubit_lost == False):
                            if pur_round == 0: #First round
                                #trigger all sources in the path
                                self.signal_sources(index=[1,2])

                                evexpr_protocol = (self.await_port_input(self._portleft_1) & \
                                    self.await_signal(self.subprotocols[f"CorrectProtocol_{self._path['request']}_1"], Signals.SUCCESS) &\
                                    self.await_port_input(self._portleft_2) & \
                                    self.await_signal(self.subprotocols[f"CorrectProtocol_{self._path['request']}_2"], Signals.SUCCESS))

                                timer_event = self._schedule_after(self._total_delay, evtypetimer)

                            else: #we keep the qubit in the first link and trigger EPRs in the second
                                #trigger all sources in the path
                                self.signal_sources(index=[2])

                                #Wait for qubits in both links and corrections in both
                                evexpr_protocol = (self.await_port_input(self._portleft_2) & \
                                    self.await_signal(self.subprotocols[f"CorrectProtocol_{self._path['request']}_2"], Signals.SUCCESS))

                                timer_event = self._schedule_after(self._total_delay, evtypetimer)

                            #Wait for qubits in both links and corrections in both or timer is over
                            evexpr_proto = yield evexpr_timer | evexpr_protocol

                            if evexpr_proto.second_term.value: #swapping ok
                                #unchedule timer
                                timer_event.unschedule()
                                
                                #trigger purification
                                self.send_signal(self._start_purif_signal, 0)
    
                                #wait for both ends to finish purification
                                expr_distil = yield (self.await_signal(self.subprotocols[f"DistilProtocol_{self._path['nodes'][0]}_{self._path['request']}"], self._purif_result_signal) &
                                    self.await_signal(self.subprotocols[f"DistilProtocol_{self._path['nodes'][-1]}_{self._path['request']}"], self._purif_result_signal))

                                source_protocol1 = expr_distil.second_term.atomic_source
                                ready_signal1 = source_protocol1.get_signal_by_event(
                                    event=expr_distil.second_term.triggered_events[0], receiver=self)
                                source_protocol2 = expr_distil.second_term.atomic_source
                                ready_signal2 = source_protocol2.get_signal_by_event(
                                    event=expr_distil.second_term.triggered_events[0], receiver=self)
                                
                                #if both SUCCESS signals have result 0, purification has succeeded
                                #if any has value not equal to cero, purification must be restarted
                                if ready_signal1.result == 0 and ready_signal2.result ==0:
                                    purification_done = True
                                else:
                                    #self.start_subprotocols()
                                    #restart purification from beggining
                                    purification_done = False
                                    break 
                            else: 
                                #qubit is lost, must restart round
                                #ic(f"{self.name} Lost qubit")
                                #restart correction protocol
                                self.send_signal(self._restart_signal)

                                #restart purification from beggining
                                purification_done = False
                                break
                            
                            #so far purification protocol is ok, next purification round
                            pur_round += 1

                        #if we get to this point, we have ended the fidelity estimation round
                        round_done = True

            #round is done we measure fidelity
            if round_done:# and purification_done:
                self.send_signal(Signals.SUCCESS)
        
class SwapProtocol(NodeProtocol):
    """Perform Swap on a repeater node.
    Adapted from NetSquid web examples

    Parameters
    ----------
    node : :class:`~netsquid.nodes.node.Node` or None, optional
        Node this protocol runs on.
    name : str
        Name of this protocol.

    """

    def __init__(self, node, mem_left, mem_right, name, request):
        super().__init__(node, name)

        # get index of link
        div_pos = name.rfind('_')
        self._index = name[div_pos+1:div_pos+2]

        self._request = request
        self._mem_left = mem_left
        self._mem_right = mem_right
        self._qmem_input_port_l = self.node.qmemory.ports[f"qin{mem_left}"]
        self._qmem_input_port_r = self.node.qmemory.ports[f"qin{mem_right}"]
        self._program = QuantumProgram(num_qubits=2)
        q1, q2 = self._program.get_qubit_indices(num_qubits=2)
        self._program.apply(INSTR_MEASURE_BELL, [q1, q2], output_key="m", inplace=False)
        

    def run(self):
        #Get instruction duration for timer. Minimum is 100
        max_duration = 100
        for inst in self.node.qmemory.get_physical_instructions():
            if inst.duration > max_duration:
                max_duration = inst.duration
        timer_duration = max_duration*0.1 if max_duration > 1000 else 100

        while True:
                    
            yield (self.await_port_input(self._qmem_input_port_l) &
                   self.await_port_input(self._qmem_input_port_r))

            #Add to node queue
            self.node.add_request(self.name)
    
            #More than two requests can arrive at the same time to qprocessor
            not_serviced = True
            while not_serviced:
                
                if self.name == self.node.get_request('first'): #First in queue, can be serviced   

                    #Check for future removal. We manage qprocessor with FIFO queue
                    # Perform Bell measurement
                    #if self.node.qmemory.busy:
                    #    yield self.await_program(self.node.qmemory)

                    yield self.node.qmemory.execute_program(self._program, qubit_mapping=[self._mem_right, self._mem_left])
                    #Serviced, remove from queue
                    self.node.remove_request('first')
                    not_serviced = False
                else: #Must wait for other to complete
                    yield self.await_timer(duration=timer_duration) #Nothing to do, just wait

            m, = self._program.output["m"]
            # Send result to right node on end
            self.node.ports[f"ccon_R_{self.node.name}_{self._request}_{self._index}"].tx_output(Message(m))
            
class SwapCorrectProgram(QuantumProgram):
    """Quantum processor program that applies all swap corrections."""
    default_num_qubits = 1

    def set_corrections(self, x_corr, z_corr):
        self.x_corr = x_corr % 2
        self.z_corr = z_corr % 2

    def program(self):
        q1, = self.get_qubit_indices(1)
        if self.x_corr == 1:
            self.apply(INSTR_X, q1)
        if self.z_corr == 1:
            self.apply(INSTR_Z, q1)
        yield self.run()


class CorrectProtocol(NodeProtocol):
    """Perform corrections for a swap on an end-node.
    Adapted from NetSquid web examples

    Parameters
    ----------
    node : :class:`~netsquid.nodes.node.Node` or None, optional
        Node this protocol runs on.
    num_nodes : int
        Number of nodes in the repeater chain network.

    """
    def __init__(self, node, mempos, num_nodes, name, request,restart_expression,epr_state):
        super().__init__(node, name)
        self._mempos = mempos
        self.num_nodes = num_nodes
        self._request = request
        self._epr_state = epr_state

        # get index of link
        div_pos = name.rfind('_')
        self._index = name[div_pos+1:div_pos+2]

        self._x_corr = 0
        self._z_corr = 0
        self._program = SwapCorrectProgram()
        self._counter = 0

        #Add restart signal. Needed when purification is used and one quit is lost
        self._restart_signal = 'RESTART_CORRECT_PROTOCOL'
        self.add_signal(self._restart_signal)
        

        self._restart_expression = restart_expression

    def run(self):
        from network import EndNode
        qubit_ready = False
        
        while True:
            message = None
            #Wait for:
            #      - a classical signal to arrive (correction) or
            #      - a qubit to be stored in memory or
            #      - or a request from main protocol to restart
            expr = yield (self.await_port_input(self.node.ports[f"ccon_L_{self.node.name}_{self._request}_{self._index}"]) | \
                self.await_port_input(self.node.qmemory.ports[f"qin{self._mempos}"]))|\
                self._restart_expression

            if expr.first_term.value:
                for received_event in expr.triggered_events:
                    if isinstance(received_event.source.component,QuantumProcessor) == True:
                        #The message that has arrived corresponds to a qubit in memory from source
                        qubit_ready = True
                    elif isinstance(received_event.source.component,EndNode) == True:
                        #Message is a classical corresponding to corrections
                        message = self.node.ports[f"ccon_L_{self.node.name}_{self._request}_{self._index}"].rx_input()
            
                if message is not None: 
                    #Port can receive more than one classical message at the same time
                    for m in message.items:
                        if self._epr_state == 'PHI_PLUS':
                            if m == ks.BellIndex.B01 or m == ks.BellIndex.B11:
                                self._x_corr += 1
                            if m == ks.BellIndex.B10 or m == ks.BellIndex.B11:
                                self._z_corr += 1
                        else:
                            if m == ks.BellIndex.B10 or m == ks.BellIndex.B00:
                                self._x_corr += 1
                            if m == ks.BellIndex.B10 or m == ks.BellIndex.B11:
                                self._z_corr += 1

                        self._counter += 1
                
                #When all switches corrections have arrived and also we have a qubit in memory            
                if self._counter == self.num_nodes - 2 and qubit_ready:
                    if self._x_corr or self._z_corr:
                        self._program.set_corrections(self._x_corr, self._z_corr)
                        if self.node.qmemory.busy:
                            yield self.await_program(self.node.qmemory)
                        yield self.node.qmemory.execute_program(self._program, qubit_mapping=[self._mempos])
                    qubit_ready = False
                    self._x_corr = 0
                    self._z_corr = 0
                    self._counter = 0
                    self.send_signal(Signals.SUCCESS)

            else: 
                #qubit is lost in one of the two links when purifying, restart correct protocol
                self._x_corr = 0
                self._z_corr = 0
                self._counter = 0
                qubit_ready = False

class DistilProtocol(NodeProtocol):
    """Protocol that does local DEJMPS distillation on a node.

    This is done in combination with another node.
    Adapted from available example by NetSquid
    Even though in actual implementation memory positions in end nodes are always 0 and 1,
    we get those positions in order to reuse it in a future development
    """
    # set basis change operators for local DEJMPS step
    _INSTR_Rx = IGate("Rx_gate", ops.create_rotation_op(np.pi / 2, (1, 0, 0)))
    _INSTR_RxC = IGate("RxC_gate", ops.create_rotation_op(np.pi / 2, (1, 0, 0), conjugate=True))

    def __init__(self, node, port, role, mem_pos1, mem_pos2, start_expression=None, msg_header="distil", name=None):
        if role.upper() not in ["A", "B"]:
            raise ValueError
        conj_rotation = role.upper() == "B"
        if not isinstance(port, Port):
            raise ValueError("{} is not a Port".format(port))
        name = name if name else "DistilNode({}, {})".format(node.name, port.name)
        super().__init__(node, name=name)
        self.port = port

        self.start_expression = start_expression
        self._program = self._setup_dejmp_program(conj_rotation)
        #self.INSTR_ROT = self._INSTR_Rx if not conj_rotation else self._INSTR_RxC
        self.local_qcount = 0
        self.local_meas_result = None
        self.remote_qcount = 0
        self.remote_meas_result = None
        self.header = msg_header
        self._qmem_positions = [None, None]
        self._waiting_on_second_qubit = False
        self._mem_pos1 = mem_pos1
        self._mem_pos2 = mem_pos2
        if start_expression is not None and not isinstance(start_expression, EventExpression):
            raise TypeError("Start expression should be a {}, not a {}".format(EventExpression, type(start_expression)))

        #Define new signal that will be used to inform routing protocol of purification result
        self._result_signal = 'PURIF_DONE'
        self.add_signal(self._result_signal)
        #Define new signal that triggers distil protocol
        self._start_purif_signal = 'START_PURIFICATION'
        self.add_signal(self._start_purif_signal)

    def _setup_dejmp_program(self, conj_rotation):
        INSTR_ROT = self._INSTR_Rx if not conj_rotation else self._INSTR_RxC
        prog = QuantumProgram(num_qubits=2)
        q1, q2 = prog.get_qubit_indices(2)
        prog.apply(INSTR_ROT, [q1])
        prog.apply(INSTR_ROT, [q2])
        prog.apply(INSTR_CNOT, [q1, q2])
        prog.apply(INSTR_MEASURE, q2, output_key="m", inplace=False)
        return prog

    def run(self):
        cchannel_ready = self.await_port_input(self.port)
        qswap_ready = self.start_expression
        while True:

            expr = yield cchannel_ready | qswap_ready

            if expr.first_term.value:
                classical_message = self.port.rx_input(header=self.header)
                if classical_message:
                    self.remote_qcount, self.remote_meas_result = classical_message.items
            elif expr.second_term.value:
                source_protocol = expr.second_term.atomic_source
                # nothing to be done with value of signal, but we keep it in case is needed in the future
                ready_signal = source_protocol.get_signal_by_event(
                    event=expr.second_term.triggered_events[0], receiver=self)

                #yield from self._handle_new_qubit(ready_signal.result)
                yield from self._handle_new_qubit(self._mem_pos1)
                yield from self._handle_new_qubit(self._mem_pos2)
            self._check_success()

    def start(self):
        # Clear any held qubits
        self._clear_qmem_positions()
        self.local_qcount = 0
        self.local_meas_result = None
        self.remote_qcount = 0
        self.remote_meas_result = None
        self._waiting_on_second_qubit = False
        return super().start()

    def _clear_qmem_positions(self):
        positions = [pos for pos in self._qmem_positions if pos is not None]
        if len(positions) > 0:
            self.node.qmemory.pop(positions=positions)
        self._qmem_positions = [None, None]

    def _handle_new_qubit(self, memory_position):
        # Process signalling of new entangled qubit
        assert not self.node.qmemory.get_position_empty(memory_position)
        if self._waiting_on_second_qubit:
            # Second qubit arrived: perform distil
            assert not self.node.qmemory.get_position_empty(self._qmem_positions[self._mem_pos1])
            assert memory_position != self._qmem_positions[self._mem_pos1]
            self._qmem_positions[1] = memory_position
            self._waiting_on_second_qubit = False
            yield from self._node_do_DEJMPS()
        else:
            # New candidate for first qubit arrived
            # Pop previous qubit if present:
            pop_positions = [p for p in self._qmem_positions if p is not None and p != memory_position]
            if len(pop_positions) > 0:
                self.node.qmemory.pop(positions=pop_positions)
            # Set new position:
            self._qmem_positions[self._mem_pos1] = memory_position
            self._qmem_positions[self._mem_pos2] = None
            self.local_qcount += 1
            self.local_meas_result = None
            self._waiting_on_second_qubit = True

    def _node_do_DEJMPS(self):
        # Perform DEJMPS distillation protocol locally on one node
        pos1, pos2 = self._qmem_positions
        if self.node.qmemory.busy:
            yield self.await_program(self.node.qmemory)
        # We perform local DEJMPS
        yield self.node.qmemory.execute_program(self._program, [pos1, pos2])  # If instruction not instant
        self.local_meas_result = self._program.output["m"][0]
        self._qmem_positions[self._mem_pos2] = None
        # Send local results to the remote node to allow it to check for success.
        self.port.tx_output(Message([self.local_qcount, self.local_meas_result],
                                    header=self.header))

    def _check_success(self):
        # Check if distillation succeeded by comparing local and remote results
        if (self.local_qcount == self.remote_qcount and
                self.local_meas_result is not None and
                self.remote_meas_result is not None):
            if self.local_meas_result == self.remote_meas_result:
                #SUCCESS
                #self.send_signal(Signals.SUCCESS, self._qmem_positions[0])
                #self.send_signal(Signals.SUCCESS, 0)
                self.send_signal(self._result_signal, 0)
            else:
                # FAILURE
                self._clear_qmem_positions()
                #self.send_signal(Signals.FAIL, self.local_qcount)
                #self.send_signal(Signals.SUCCESS, 1)
                self.send_signal(self._result_signal, 1)

            self.local_meas_result = None
            self.remote_meas_result = None
            self._qmem_positions = [None, None]

    @property
    def is_connected(self):
        if self.start_expression is None:
            return False
        if not self.check_assigned(self.port, Port):
            return False
        if not self.check_assigned(self.node, Node):
            return False
        if self.node.qmemory.num_positions < 2:
            return False
        return True
