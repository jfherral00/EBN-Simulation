from utils import dc_setup
from netsquid.protocols import LocalProtocol, NodeProtocol, Signals
from netsquid.components.component import Message
from icecream import ic
import numpy as np

from netsquid.util.simtools import sim_time
from netsquid.qubits import qubitapi as qapi, create_qubits, assign_qstate
from netsquid.qubits import ketstates as ks
from protocols import RouteProtocol
from netsquid.qubits import set_qstate_formalism, QFormalism
from netsquid.components.instructions import INSTR_MEASURE_BELL, INSTR_CNOT, INSTR_CCX, INSTR_H
from netsquid.components import QuantumProgram
from protocols import SwapCorrectProgram
from network import ClassicalConnection
from netsquid.components.models.delaymodels import FixedDelayModel, FibreDelayModel, GaussianDelayModel
import cmath
from random import randint
from netsquid.qubits.operators import Operator, X, Z, I

'''
Available applications:
    - Capacity: Will generate end to end entanglement during the simulation and will measure the 
    maximum generation rate for each request.
    - Teleportation: Will teleport continuously the list of qubits. After one teleportation,
    a new one will start. Will measure fidelity and the amount of teleported qubits.
    - QBER: Will teleport 0's and 1's, coded as |0> and |1>, and will measure the bit errors.
    - TeleportationWithDemand: Will teleport the list of qubits, but following the specified generation
    rate. Will measure the mean fidelity and total number of teleported qubits, but also the size 
    of the queue at the source node.
    - CHSH: Experiments to validate Bell inequalities
'''

class GeneralApplication(LocalProtocol):
    '''
    Superclass of all applications.  Will instantiate RouteProtocol and Datacollector.
    Constructor parameters:
        - path: dictionary with path parameters
        - netwokmanager: instance of the network manager class being used in the simulation
        - name: string, name of the instance
    '''

    def __init__(self, path, networkmanager, name=None):
        #Signal that asks for entanglement
        self._ent_request = 'START_ENTANGLEMENT'
        self.add_signal(self._ent_request)

        self.name = name if name else f"Application_Unidentified"
        super().__init__(nodes=networkmanager.network.nodes, name=name)
        self._path = path
        self._networkmanager = networkmanager

        ent_start_expression = self.await_signal(self, self._ent_request)
        self.add_subprotocol(RouteProtocol(networkmanager,path,ent_start_expression,phase='application',purif_rounds=path['purif_rounds']))

        #Initialize data collector that will gather metrics
        self.dc = dc_setup(self)
    
class CapacityApplication(GeneralApplication):
    '''
    This class implements and application that generates end to end entanglement continously
    and measures the fidelity of the entangled pairs
    Constructor parameters:
        - path: dictionary with path parameters
        - netwokmanager: instance of the network manager class being used in the simulation
        - name: string, name of the instance
    '''

    def __init__(self, path, networkmanager, name=None):
        name = name if name else f"CapacityApplication_Unidentified"
        super().__init__(path, networkmanager, name=name)
    
    def run(self):
        self.start_subprotocols()

        #Get type of EPR to use
        epr_state = ks.b00 if self._networkmanager.get_config('epr_pair','epr_pair') == 'PHI_PLUS' else ks.b01

        #Though in this simulations positions in nodes are always 0, we query in case this is changed in the future
        first_link = self._path['comms'][0]['links'][0]
        last_link = self._path['comms'][-1]['links'][0]
        mem_posA_1 = self._networkmanager.get_mem_position(self._path['nodes'][0],first_link.split('-')[0],first_link.split('-')[1])
        mem_posB_1 = self._networkmanager.get_mem_position(self._path['nodes'][-1],last_link.split('-')[0],last_link.split('-')[1])

        while True:
            start_time = sim_time()
            #Send signal for entanglement generation
            self.send_signal(self._ent_request)

            #Wait for  entanglement to be generated on both ends
            yield self.await_signal(self.subprotocols[f"RouteProtocol_{self._path['request']}"],Signals.SUCCESS)

            #Measure fidelity and send metrics to datacollector
            #if self._networkmanager.network.get_node(self._path['nodes'][0]).qmemory.busy:
            #    yield self.await_program(self._networkmanager.network.get_node(self._path['nodes'][0]).qmemory)
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


class TeleportationApplication(GeneralApplication):
    '''
    Class that implements three type of applications:
        Teleportation: Will teleport one qubit after another, without an input demand rate.
                 Qubits will be teleported as soon as the previous one has been transmitted.
        TeleportationWithDemand: Origin node will generate qubits with a specified rate.
                 They will be stored in a local queue and sent as a slot is available.
        QBER: Measure binary digits encoded in quantum states
    '''

    def __init__(self, path, networkmanager, qubits, epr_pair, app, rate = 0, name=None):
        name = name if name else f"TeleportApplication_Unidentified"
        super().__init__(path, networkmanager, name=name)

        self._qubits = qubits
        self._app = app

        mem_posB_1 = self._networkmanager.get_mem_position(self._path['nodes'][-1],self._path['comms'][-1]['links'][0].split('-')[0],0)
        #mem_posB_1=0
        self.add_subprotocol(TeleportCorrectProtocol(networkmanager.network.get_node(path['nodes'][-1]),mem_posB_1,f"TeleportCorrectProtocol_{path['request']}",path['request'],epr_pair))

        self._build_teleport_classic()

        if app == 'TeleportationWithDemand': #Request demand is modelled
            teleport_strategy = networkmanager.get_config('nodes',self._path['nodes'][0],'teleport_strategy')
            self.add_subprotocol(DemandGeneratorProtocol(networkmanager.network.get_node(path['nodes'][0]),rate,qubits,teleport_strategy,f"DemandGeneratorProtocol_{path['request']}"))

    def _build_teleport_classic(self):
        '''
        Adds classical channels for Teleportation protocol
        '''
        for nodepos in range(len(self._path['nodes'])-1):
            link = self._networkmanager.get_link(self._path['nodes'][nodepos],self._path['nodes'][nodepos+1],next_index=False)

            #Get classical channel delay model
            classical_delay_model = None
            fibre_delay_model = self._networkmanager.get_config('links',link, 'classical_delay_model')
            if fibre_delay_model == 'NOT_FOUND' or fibre_delay_model == 'FibreDelayModel':
                classical_delay_model = FibreDelayModel(c=float(self._networkmanager.get_config('links',link,'photon_speed_fibre')))
            elif fibre_delay_model == 'GaussianDelayModel':
                classical_delay_model = GaussianDelayModel(delay_mean=float(self._networkmanager.get_config('links',link,'gaussian_delay_mean')),
                    delay_std = float(self._networkmanager.get_config('links',link,'gaussian_delay_std')))
            else: # In case other, we assume FibreDelayModel
                classical_delay_model = FibreDelayModel(c=float(self._networkmanager.get_config('links',link,'photon_speed_fibre')))

            #Create classical connection for each link
            cconn = ClassicalConnection(name=f"cconn_{self._path['nodes'][nodepos]}_{self._path['nodes'][nodepos+1]}_{self._path['request']}_teleport", 
                length=self._networkmanager.get_config('links',link,'distance'))
            cconn.subcomponents['Channel_A2B'].models['delay_model'] = classical_delay_model

            port_name, port_r_name = self._networkmanager.network.add_connection(
                self._networkmanager.network.get_node(self._path['nodes'][nodepos]), 
                self._networkmanager.network.get_node(self._path['nodes'][nodepos+1]), 
                connection=cconn, label=f"cconn_{self._path['nodes'][nodepos]}_{self._path['nodes'][nodepos+1]}_{self._path['request']}_teleport",
                port_name_node1=f"ccon_R_{self._path['nodes'][nodepos]}_{self._path['request']}_teleport", 
                port_name_node2=f"ccon_L_{self._path['nodes'][nodepos+1]}_{self._path['request']}_teleport")

            #Forward cconn to right most node
            if f"ccon_L_{self._path['nodes'][nodepos]}_{self._path['request']}_teleport" in self._networkmanager.network.get_node(self._path['nodes'][nodepos]).ports:
                self._networkmanager.network.get_node(self._path['nodes'][nodepos]).ports[f"ccon_L_{self._path['nodes'][nodepos]}_{self._path['request']}_teleport"].bind_input_handler(self._handle_message,tag_meta=True)    

    def _handle_message(self,msg):
        input_port = msg.meta['rx_port_name']
        forward_port = input_port.replace('ccon_L_','ccon_R_')
        port_elements = input_port.split('_')
        node = self._networkmanager.network.get_node(port_elements[2])
        node.ports[forward_port].tx_output(msg)
        return
    
    def run(self):
        if self._app == 'LogicalTeleportation': #Shor coding/uncoding
            codingprogram = ShorCodingProgram()
            decodingprogram = ShorDecodingProgram()
            coded_qubit = False #Initially no logical qubit available
            
        self.start_subprotocols()

        #Though in this simulations positions in nodes are always 0, we query in case this is changed in the future
        first_link = self._path['comms'][0]['links'][0]
        last_link = self._path['comms'][-1]['links'][0]
        mem_posA_1 = self._networkmanager.get_mem_position(self._path['nodes'][0],first_link.split('-')[0],first_link.split('-')[1])
        mem_posB_1 = self._networkmanager.get_mem_position(self._path['nodes'][-1],last_link.split('-')[0],last_link.split('-')[1])
        mem_posTeleport = 2

        #Init measurement program
        self._program = QuantumProgram(num_qubits=2)
        q1, q2 = self._program.get_qubit_indices(num_qubits=2)
        self._program.apply(INSTR_MEASURE_BELL, [q1, q2], output_key="m", inplace=False)

        set_qstate_formalism(QFormalism.KET)
        qubit = create_qubits(1)
        original_qubit = create_qubits(1)

        #Get first node (get the qubit to transmit) and last_node (check if teleport
        #protocol has succeeded)
        first_node = self._networkmanager.network.get_node(self._path['nodes'][0])
        last_node = self._networkmanager.network.get_node(self._path['nodes'][-1])

        #Initialize position of qubit to transmit
        num_qubits = len(self._qubits) #Number of qubits to teleport
        tx_qubit = 0 #Position of qubit to transmit

        #Get teleportation strategy if implementing Demand
        if self._app == 'TeleportationWithDemand':
            teleport_strategy = self._networkmanager.get_config('nodes',self._path['nodes'][0],'teleport_strategy')

        while True:
        
            if self._app in ['Teleportation','QBER']:
                #No demand, we'll request as soon as the is a slot
                #Get state to teleport. First normalize it
                alpha = complex(self._qubits[tx_qubit][0])
                beta = complex(self._qubits[tx_qubit][1])
                norm = cmath.sqrt(np.conjugate(alpha)*alpha + np.conjugate(beta)*beta)
                state = np.array([[alpha], [beta]], dtype=complex)/norm

                #Set position of next qubit to transmit
                tx_qubit = tx_qubit + 1 if tx_qubit < num_qubits-1 else 0

                assign_qstate(qubit, state)

            elif self._app == 'TeleportationWithDemand':
                #We have to request a qubit based on generation demand
                waiting_state = True
                while waiting_state:
                    #retrieve state from queue. We get the state reprsentation in return, 
                    # but also qubit is updated with the retrieved qubit
                    [state, qubit[0]] = first_node.retrieve_teleport(teleport_strategy)

                    #If no qubit to transmit, wait 1000 nanoseconds
                    if state is None:
                        #Wait for a qubit to be ready for teleportation
                        yield self.await_timer(1000)
                    else:
                        #We have a qubit ready for teleportation
                        waiting_state = False
                        
            elif self._app == 'LogicalTeleportation':
                if not coded_qubit:
                    #Build state
                    alpha = complex(self._qubits[tx_qubit][0])
                    beta = complex(self._qubits[tx_qubit][1])
                    norm = cmath.sqrt(np.conjugate(alpha)*alpha + np.conjugate(beta)*beta)
                    state = np.array([[alpha], [beta]], dtype=complex)/norm
                    logical_qubit_pos = 1
                    coded_qubit = True

                    #Build qubit from state and assign it to memory position 4
                    trans_qubit = create_qubits(9)

                    #Assign state to first physical qubit
                    assign_qstate(trans_qubit[0], state)
                    #Discard qubits from positions. If memory have noise, setting put with
                    # replace = True won't work. We must discard first.
                    try: 
                        first_node.qmemory.discard([4,5,6,7,8,9,10,11,12])
                    except: #The first time there is no qubit
                        pass
                    first_node.qmemory.put(trans_qubit, [4,5,6,7,8,9,10,11,12], replace = False)
                    
                    #Code physical qubit into logical
                    yield first_node.qmemory.execute_program(codingprogram, qubit_mapping=[4,5,6,7,8,9,10,11,12])
                    
                    #Get first qubit to be transmitted
                    qubit = first_node.qmemory.peek(logical_qubit_pos+3, skip_noise=False)
                    
                    #Set start of logical teleportation
                    logical_start_time = sim_time()
                else:
                    #get next coded qubit that must be transmitted
                    logical_qubit_pos += 1
                    qubit = first_node.qmemory.peek(logical_qubit_pos+3, skip_noise=False)
                    
            #Start time measurement
            start_time = sim_time()

            #If position is not being used, we can store the qubit
            if 2 in self._networkmanager.network.get_node(self._path['nodes'][0]).qmemory.unused_positions:
                #store qubit un memory position
                first_node.qmemory.put(qubit, 2, replace = False)
 
                #Request entanglement to RouteProtocol
                self.send_signal(self._ent_request)
                
                #Wait for  entanglement to be generated on both ends
                yield self.await_signal(self.subprotocols[f"RouteProtocol_{self._path['request']}"],Signals.SUCCESS)
                
                #Measure in Bell basis positions 0 and 2
                yield first_node.qmemory.execute_program(self._program, qubit_mapping=[mem_posTeleport,mem_posA_1])
                m, = self._program.output["m"]
                
                # Send result to right node on end
                first_node.ports[f"ccon_R_{self._path['nodes'][0]}_{self._path['request']}_teleport"].tx_output(Message(m))

                #Wait for Teleportation to complete
                yield self.await_signal(self.subprotocols[f"TeleportCorrectProtocol_{self._path['request']}"],Signals.SUCCESS)

                result_qubit, = last_node.qmemory.pop(0)

                if self._app in ['Teleportation']:
                    fid = qapi.fidelity(result_qubit, state, squared = True)
                    qapi.discard(result_qubit)
                    result = {
                        'posA': mem_posA_1,
                        'posB': mem_posB_1,
                        'Fidelity': fid,
                        'time': sim_time() - start_time
                    }

                elif self._app in ['TeleportationWithDemand']:
                    fid = qapi.fidelity(result_qubit, state, squared = True)
                    qapi.discard(result_qubit)
                    result = {
                        'posA': mem_posA_1,
                        'posB': mem_posB_1,
                        'Fidelity': fid,
                        'time': sim_time() - start_time,
                        'queue_size': first_node.get_queue_size(),
                        'discarded_qubits': first_node.get_discarded()
                    }
    
                elif self._app == 'QBER':
                    #In result_qubit the teleported one
                    assign_qstate(original_qubit, state)

                    #Measure original qubit and teleported one in Z basis and compare
                    m_origin,prob_or = qapi.measure(original_qubit[0])
                    m_res,prob_res = qapi.measure(result_qubit)
                    error = 1 if m_origin != m_res else 0

                    qapi.discard(result_qubit)
                    result = {
                        'error': error,
                        'time': sim_time() - start_time
                    }
                    
                elif self._app == 'LogicalTeleportation':
                    if logical_qubit_pos == 9:
                        #Get last teleported qubit
                        last_node.qmemory.put(result_qubit, logical_qubit_pos+3, replace = True)
                        
                        #Apply decoding circuit
                        yield last_node.qmemory.execute_program(decodingprogram, qubit_mapping=[4,5,6,7,8,9,10,11,12])
                        
                        #Get qubit in position 4, whith decoded state
                        result_qubit, = last_node.qmemory.pop(4)
                        
                        #Measure fidelity with respect to original state
                        fid = qapi.fidelity(result_qubit, state, squared = True)
                        qapi.discard(result_qubit)
                        result = {
                            'posA': mem_posA_1,
                            'posB': mem_posB_1,
                            'Fidelity': fid,
                            'time': sim_time() - logical_start_time
                        }
                        #Prepare for next logical qubit
                        coded_qubit = False
                    else: #Nothing to do, must teleport next physical qubit
                        #get qubit in position 0 an move it to memory position for decoding
                        last_node.qmemory.put(result_qubit, logical_qubit_pos+3, replace = True)
                        continue #do not execute send_signal to datacollector yet

                #send result to datacollector
                self.send_signal(Signals.SUCCESS, result) 
            else:
                self.await_timer(1000)

class DemandGeneratorProtocol(NodeProtocol):
    '''
    Class that implements the protocol generating qubits following
    the specified demand rate
    Parameters
    ------------------------
    node: EndNode instance where is is running
    rate: demand_rate in Hz
    qubits: list of states to teleport. Once at the end, it will start from the begginning
    name: name of protocol
    '''
    def __init__(self, node, rate, qubits, teleport_strategy, name=None):
        name = name if name else f"DemandGenerator_Unidentified"
        super().__init__(node, name)
        self._time_between_states = 1e9 / rate
        self._qubits = qubits
        self._teleport_strategy = teleport_strategy
    
    def run(self):
        num_qubits = len(self._qubits) #Number of qubits to teleport
        tx_qubit = 0 #Position of qubit to transmit
        while True:
            #Wait based on demand rate
            yield self.await_timer(self._time_between_states)
            
            #Get state to teleport. First normalize it
            alpha = complex(self._qubits[tx_qubit][0])
            beta = complex(self._qubits[tx_qubit][1])
            norm = cmath.sqrt(np.conjugate(alpha)*alpha + np.conjugate(beta)*beta)
            state = np.array([[alpha], [beta]], dtype=complex)/norm

            #Set position of next qubit to transmit
            tx_qubit = tx_qubit + 1 if tx_qubit < num_qubits-1 else 0
            
            #Add qubit to queue in origin
            self.node.request_teleport(state, self._teleport_strategy)


class TeleportCorrectProtocol(NodeProtocol):
    """Perform corrections for a swap on an end-node.
    Adapted from NetSquid web examples

    Parameters
    ----------
    node : :class:`~netsquid.nodes.node.Node` or None, optional
        Node this protocol runs on.
    num_nodes : int
        Number of nodes in the repeater chain network.

    """
    def __init__(self, node, mempos, name, request,epr_state):
        super().__init__(node, name)
        self._mempos = mempos
        self._request = request
        self._epr_state = epr_state

        self._x_corr = 0
        self._z_corr = 0

        self._program = SwapCorrectProgram()
   

    def run(self):
        qubit_ready = False
        corrections = 0
        
        while True:
            message = None
            #Wait for a classical signal to arrive and a qubit at the destination memory
            expr = yield self.await_port_input(self.node.ports[f"ccon_L_{self.node.name}_{self._request}_teleport"]) | \
                self.await_port_input(self.node.qmemory.ports[f"qin{self._mempos}"])
                
            if expr.first_term.value:
                message = self.node.ports[f"ccon_L_{self.node.name}_{self._request}_teleport"].rx_input()
            else:
                qubit_ready = True

            if message is not None:
                m = message.items[0]
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
                corrections += 1
                        
            if corrections and qubit_ready:
                if self._x_corr or self._z_corr:
                    self._program.set_corrections(self._x_corr, self._z_corr)
                    if self.node.qmemory.busy:
                        yield self.await_program(self.node.qmemory)
                    yield self.node.qmemory.execute_program(self._program, qubit_mapping=[self._mempos])
                
                self._x_corr = 0
                self._z_corr = 0
                corrections = 0
                qubit_ready = False
                self.send_signal(Signals.SUCCESS)
                
                
                

class CHSHApplication(GeneralApplication):
    '''
    This class implements and application that generates end to end entanglement continously
    and measures the fidelity of the entangled pairs
    Constructor parameters:
        - path: dictionary with path parameters
        - netwokmanager: instance of the network manager class being used in the simulation
        - name: string, name of the instance
    '''

    def __init__(self, path, networkmanager, name=None):
        name = name if name else f"CHSHApplication_Unidentified"
        super().__init__(path, networkmanager, name=name)
    
    def run(self):
        self.start_subprotocols()

        #Though in this simulations positions in nodes are always 0, we query in case this is changed in the future
        first_link = self._path['comms'][0]['links'][0]
        last_link = self._path['comms'][-1]['links'][0]
        mem_posA_1 = self._networkmanager.get_mem_position(self._path['nodes'][0],first_link.split('-')[0],first_link.split('-')[1])
        mem_posB_1 = self._networkmanager.get_mem_position(self._path['nodes'][-1],last_link.split('-')[0],last_link.split('-')[1])

        #Define Alices's operators
        if self._networkmanager.get_config('epr_pair','epr_pair') == 'PHI_PLUS':
            A0 = Z
            A1 = X
        else:
            A0 = Z
            A1 = X

        #Define Bob's operators
        if self._networkmanager.get_config('epr_pair','epr_pair') == 'PHI_PLUS':
            B0 = (1/np.sqrt(2))*(X+Z)
            B1 = (1/np.sqrt(2))*(Z-X)
        else:
            B0 = (1/np.sqrt(2))*(I-Z+X)
            B1 = (1/np.sqrt(2))*(I-Z-X)

        while True:
            start_time = sim_time()
            #Send signal for entanglement generation
            self.send_signal(self._ent_request)

            #Wait for  entanglement to be generated on both ends
            yield self.await_signal(self.subprotocols[f"RouteProtocol_{self._path['request']}"],Signals.SUCCESS)

            #Generate x and y, which will be use for Alice and Bob measurements
            x = randint(0,1)
            y = randint(0,1)

            qa, = self._networkmanager.network.get_node(self._path['nodes'][0]).qmemory.pop(positions=[mem_posA_1])
            qb, = self._networkmanager.network.get_node(self._path['nodes'][-1]).qmemory.pop(positions=[mem_posB_1])
            
            #Measure Alice's qubit. In Z base if x=0 or X if x = 1
            observable = A0 if x == 0 else A1
            measure_a,prob_a = qapi.measure(qa, observable=observable)

            #Measure Bob's qubit.
            observable = B0 if y == 0 else B1
            measure_b,prob_b = qapi.measure(qb, observable=observable)

            #Check if a xor b = x * y
            wins = 1 if (measure_a + measure_b) % 2 == x * y else 0
            result = {
                'wins': wins,
                'time': sim_time() - start_time
            }

            #send result to datacollector
            self.send_signal(Signals.SUCCESS, result)
            
class ShorCodingProgram(QuantumProgram):
    '''
    This program take the qubit and codes it into Shor code, using 9 qubits
    '''
    default_num_qubits = 9
    
    def program(self):
        
        q1, q2, q3, q4, q5, q6, q7, q8, q9 = self.get_qubit_indices(9)
        self.apply(INSTR_CNOT, [q1,q4])
        self.apply(INSTR_CNOT, [q1,q7])
        self.apply(INSTR_H, q1)
        self.apply(INSTR_H, q4)
        self.apply(INSTR_H, q7)
        self.apply(INSTR_CNOT, [q1,q2])
        self.apply(INSTR_CNOT, [q4,q5])
        self.apply(INSTR_CNOT, [q7,q8])
        self.apply(INSTR_CNOT, [q1,q3])
        self.apply(INSTR_CNOT, [q4,q6])
        self.apply(INSTR_CNOT, [q7,q9])
        yield self.run()
    
class ShorDecodingProgram(QuantumProgram):
    '''
    This program take the 9 qubits and decodes into original qubit
    '''
    default_num_quits = 9
    
    def program(self):
        q1, q2, q3, q4, q5, q6, q7, q8, q9 = self.get_qubit_indices(9)
        
        self.apply(INSTR_CNOT, [q1,q2])
        self.apply(INSTR_CNOT, [q4,q5])
        self.apply(INSTR_CNOT, [q7,q8])
        self.apply(INSTR_CNOT, [q1,q3])
        self.apply(INSTR_CNOT, [q4,q6])
        self.apply(INSTR_CNOT, [q7,q9])
        self.apply(INSTR_CCX, [q2,q3,q1])
        self.apply(INSTR_CCX, [q5,q6,q4])
        self.apply(INSTR_CCX, [q9,q8,q7])
        self.apply(INSTR_H, q1)
        self.apply(INSTR_H, q4)
        self.apply(INSTR_H, q7)
        self.apply(INSTR_CNOT, [q1,q4])
        self.apply(INSTR_CNOT, [q1,q7])
        self.apply(INSTR_CCX, [q7,q4,q1])
        yield self.run()