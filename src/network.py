from icecream import ic
import netsquid as ns
import networkx as nx
import numpy as np
import smf_coupling as smf
import pandas as pd
from matplotlib import pyplot as plt
import cn2
import transmittance
import random
from scipy.integrate import quad, quad_vec
from scipy.special import i0, i1, erf
from numpy.random import weibull
from netsquid.nodes import Node, Connection, Network
from netsquid.components import Message, QuantumProcessor, QuantumProgram, PhysicalInstruction
from netsquid.qubits.state_sampler import StateSampler
from netsquid.components.qsource import QSource, SourceStatus
from netsquid.components.models.delaymodels import FixedDelayModel, FibreDelayModel, GaussianDelayModel
from netsquid.components.models.qerrormodels import DepolarNoiseModel, DephaseNoiseModel, T1T2NoiseModel, QuantumErrorModel, FibreLossModel
from netsquid.components import ClassicalChannel, QuantumChannel
from netsquid.nodes.connections import DirectConnection
from routing_protocols import LinkFidelityProtocol, PathFidelityProtocol
from netsquid.qubits import ketstates as ks
from netsquid.qubits.operators import Operator
from netsquid.components.instructions import INSTR_MEASURE_BELL, INSTR_MEASURE, INSTR_X, INSTR_Z,  INSTR_CNOT, IGate, INSTR_Y, INSTR_ROT_X, INSTR_ROT_Y, INSTR_ROT_Z, INSTR_H, INSTR_SWAP, INSTR_INIT, INSTR_CXDIR, INSTR_EMIT, INSTR_CCX
import netsquid.qubits.operators as ops
from netsquid.qubits import assign_qstate, create_qubits
from netsquid.qubits import qubitapi as qapi
import netsquid.util.simtools as simtools
from netsquid.util.simlog import warn_deprecated
from utils import dc_setup
from random import gauss


__all__ = [
    'FreeSpaceLossModel',
    'FixedSatelliteLossModel'
]

RE = 6371 # Earth's radius [km]
c = 299792458 
counter=0
# Zernike indices look-up table
max_n_wfs = 150 # Maximum radial index n returned by WFS 
array_of_zernike_index = smf.get_zernikes_index_range(max_n_wfs)
lut_zernike_index_pd = pd.DataFrame(array_of_zernike_index[1:], columns = ["n", "m", "j"])
lut_zernike_index_pd["j_Noll"] = smf.calculate_j_Noll(lut_zernike_index_pd["n"], lut_zernike_index_pd["m"])


class Switch(Node):
    def __init__(self,name,qmemory):
        self._swap_queue = []
        super().__init__(name,qmemory=qmemory)

    def add_request(self,request):
        '''
        Receives the protocol that wants to perform the swapping operation
        Input:
            - request: name of th requestor protocol (string)
        Output:
            No output
        '''
        self._swap_queue.append(request)

    def get_request(self,strategy):
        '''
        Retrieve an operation to execute from the queue.
        Can be the first one or the last one
        Input:
            - strategy: if first in queue should be returned or last (first|last)
        Output:
            - protocol_name: name of the protocol for which the entanglement will be executed
        '''
        protocol_name = self._swap_queue[0] if strategy == 'first' else self._swap_queue[-1]
        return(protocol_name)

    def remove_request(self,strategy):
        '''
        Delete an operation from the queue.
        Can be the first one or the last one
        Input:
            - strategy: if first in queue should be deleted or last (first|last)
        Output:
            No output
        '''
        self._swap_queue.pop(0) if strategy == 'first' else self._swap_queue.pop()  

class EndNode(Node):
    def __init__(self, name, queue_size, qmemory):
        
        self._state_transmit_queue = []
        self._mem_transmit_queue = []
        self._queue_size = queue_size
        super().__init__(name, qmemory=qmemory)
        self._discarded_states = 0


    def request_teleport(self, state, strategy):
        '''
        Insert new teleportation request. Will be inserted at the end of the list
        Strategy when retrieven the qubit will be processed in retrieve_teleport method
        Input:
            - state: list with state representation [alpha, beta]
            - strategy: teleportation strategy ('Oldest': send in FIFO, 'Newest': Send in LIFO mode)
        '''
        #Assign qubit to state representation
        qubit = create_qubits(1)
        assign_qstate(qubit,state)

        #If queue is full we should check strategy
        if len(self._state_transmit_queue) == self._queue_size:
            if strategy == 'Oldest': 
                #If we are priorizing oldest qubits, we should discard new request
                self._discarded_states += 1
            else:
                #Discard oldest request and insert new one
                self._state_transmit_queue.pop(0)
                self._state_transmit_queue.append(state)
  
                if self.qmemory.num_positions > 4: #We are using quantum memory for storage
                    # Replace memory position with oldest qubit with new one
                    mempos = self._mem_transmit_queue.pop(0)
                    #TODO: Check if we must insert validation of qprocessor being used
                    self.qmemory.put(qubit, mempos, replace = True)
                    self._mem_transmit_queue.append(mempos)

                self._discarded_states += 1
        else:
            self._state_transmit_queue.append(state)
            if self.qmemory.num_positions > 4: #We are using quantum memory for storage
                mempos_list = self.qmemory.unused_positions
                #Only positions equal or above 4 are used as storage
                mempos = min(i for i in mempos_list if i > 3)

                self.qmemory.put(qubit, mempos, replace = True)
                self._mem_transmit_queue.append(mempos)

    def retrieve_teleport(self, strategy):
        '''
        Return state and qubit that should be teleported
        Input:
            - strategy: teleportation strategy ('Oldest': send in FIFO, 'Newest': Send in LIFO mode)
        Output:
            - state: list with state representation [alpha, beta]
            - qubit: qubit
        '''
        qubit = []
        if len(self._state_transmit_queue) > 0:
            if strategy == 'Oldest': 
                #FIFO
                state = self._state_transmit_queue.pop(0)
                if self.qmemory.num_positions > 4: 
                    #Quantum memory being used
                    mempos = self._mem_transmit_queue.pop(0)
                    qubit = self.qmemory.pop(mempos, skip_noise=False)
            else: 
                #LIFO
                state = self._state_transmit_queue.pop()
                if self.qmemory.num_positions > 4: 
                    #Quantum memory being used
                    mempos = self._mem_transmit_queue.pop()
                    qubit = self.qmemory.pop(mempos, skip_noise=False)

            if self.qmemory.num_positions == 4: 
                #Working with classical memories, we code state in qubit
                assign_qstate(qubit, state)

            return([state,qubit[0]])
        else:
            return([None, None])
        
    def get_queue_size(self):
        '''
        Getter for _transmit_queue
        '''
        return(len(self._state_transmit_queue))
    
    def get_discarded(self):
        '''
        Getter for _discarded_states
        '''
        return(self._discarded_states)
        

class NetworkManager():
    '''
    The only initiallization parameter is the name of the file 
    storing all the network definition
    '''

    def __init__(self, config):
        self.network=""
        self._paths = []
        self._link_fidelities = {}
        self._memory_assignment = {}
        self._available_links = {}
        self._requests_status = []
        self._config = config

        self._create_network()
        self._measure_link_fidelity()
        self._calculate_paths()

    def get_info_report(self):
        '''
        Generates and returns information for the pdf report
        '''
        report_info = {}
        report_info['link_fidelities'] = self._link_fidelities
        report_info['requests_status'] = self._requests_status
        return(report_info)

    def get_config(self, mode, name, property=None):
        '''
        Enables configuration queries
        Input:
            - mode: ['nodes'|'links|'requests']
            - name: name of the element to query
            - property: attribute to query. If None (default), all attributes are returned
        Output:
            - value of required attribute
        '''
        if mode not in ['name','simulation_duration','epr_pair','link_fidel_rounds','path_fidel_rounds','loss_strategy','nodes','links','requests','atmospheric_parameters']:
            raise ValueError('Unsupported mode')
        else:
            elements = self._config[mode] 
            #Querying for a global property
            if mode in ['name','epr_pair','simulation_duration','link_fidel_rounds','path_fidel_rounds','loss_strategy']: 
                return (elements)
            
            #Querying for an element type
            found = False
            for element in elements:
                if list(element.keys())[0] == name:
                    if property:
                        try:  
                            return(list(element.values())[0][property])
                        except:
                            return('NOT_FOUND')
                    else:
                        return(list(element.values())[0])
            
            #name not found
            return('NOT_FOUND')

    def get_mem_position(self, node, link, serial):
        '''
        Maps node and link to memory position.
        If non assigned, creates assignment and stores it in private attribute
        If already assigned, gets memory position
        Input:
            - node: string. Name of node
            - link: string. Name of link
            - serial: integer or string. Index of link
        Output:
            -integer: memory position in the specified node to be used
        ''' 
        serial = str(serial)
        values_list = []

        if node in list(self._memory_assignment.keys()):
            node_links = self._memory_assignment[node]
            #get maximum assigned position
            for node_link in list(node_links.keys()):
                for link_serial in list(self._memory_assignment[node][node_link].keys()):
                    values_list.append(self._memory_assignment[node][node_link][link_serial])
            position = max(values_list) if len(values_list) > 0 else -1

            if link in list(node_links.keys()):
                link_serials = self._memory_assignment[node][link]
                if serial in list(link_serials.keys()):
                    position = link_serials[serial] #This is the assigned position
                else: #serial does not exists
                    #create serial with memory position the maximum for that link plus one
                    position += 1
                    self._memory_assignment[node][link][serial] = position
            else: #link does not exist
                #create link and serial. Position will be 0
                self._memory_assignment[node][link] = {}
                position += 1
                self._memory_assignment[node][link][serial] = position
        else: #node does not exist
            #create node, link, serial and position. Position will be 0
            self._memory_assignment[node] = {}
            self._memory_assignment[node][link] = {}
            self._memory_assignment[node][link][serial] = 0
            position = 0

        return(position)

    def get_paths(self):
        return self._paths
    
    def get_link(self, node1, node2, next_index = False):
        '''
        Obtains the name of the link between two nodes.
        Input:
            - node1, node2: connected nodes by the link
            - next_index: if True returns also the next available index in the link. False by default
        '''
        
        links = self._config['links']
        for link in links:
            link_name = list(link.keys())[0]
            link_props = self.get_config('links',link_name)
            if (link_props['end1'] == node1 and link_props['end2'] == node2) or (link_props['end1'] == node2 and link_props['end2'] == node1):
                if not next_index:
                    return(link_name)
                else:
                    next_index = max(self._available_links[link_name]['occupied']) + 1 \
                        if len(self._available_links[link_name]['occupied']) != 0 else 0
                    #We return the next instance if there are available
                    if self._available_links[link_name]['avail'] > 0:
                        self._available_links[link_name]['occupied'].append(next_index)
                        self._available_links[link_name]['avail'] -= 1
                        return([link_name,next_index])
                    
        #If we haven't returned no direct link between both ends
        return('NOLINK')

    def release_link(self, link_name, index):
        '''
        Returns as available the index of the specified link.
        Input:
            - link_name: link. string
            - index: index in the link to be released. Can be string or integer
        '''
        self._available_links[link_name]['avail'] += 1
        self._available_links[link_name]['occupied'].remove(int(index))

    def _create_network(self):
        '''
        Creates network elements as indicated in configuration file: nodes, links and requests
        Input: dictionary with file contents
        Output: -
        '''
        self.network = Network(self._config['name'])
        self._memory_assignment = {}
        self._available_links = {}

        #nodes creation
        switches = [] #List with all switches
        end_nodes = [] # List with all nodes
        for node in self._config['nodes']:
            name = list(node.keys())[0]
            props = list(node.values())[0]
            if props['type'] == 'switch':
                switch = Switch(name, qmemory=self._create_qprocessor(f"qproc_{name}",props['num_memories'], nodename=name))
                switches.append(switch)
            elif props['type'] == 'endNode':
                if 'teleport_queue_technology' in props.keys() and props['teleport_queue_technology'] == 'Quantum':
                    #If teleportation queue in node is implemented with quantum memories
                    num_memories = 4 + props['teleport_queue_size']
                else: #Queue is implemented with classical memories
                    num_memories = 4
                queue_size = props['teleport_queue_size'] if 'teleport_queue_technology' in props.keys() else 0
                
                endnode = EndNode(name, queue_size, qmemory=self._create_qprocessor(f"qproc_{name}",num_memories, nodename=name))
                end_nodes.append(endnode)
            else:
                raise ValueError('Undefined network element found')

        network_nodes = switches+end_nodes
        self.network.add_nodes(network_nodes)

        available_links= [self.get_config('links',list(link.keys())[0],'qchannel_loss_model') for link in self._config['links']]
        counter_1=0
        counter_2=0
        counter_3=0
        if ('AerialHorizontalChannel' in available_links) and counter_1 == 0:
            counter_1 += 1    
            aerial_channels= []
            for link in self._config['links']:
                if self.get_config('links',list(link.keys())[0],'qchannel_loss_model') == 'AerialHorizontalChannel':
                    aerial_channels.append(link)
            
            horizontal_distance=np.mean([list(link.values())[0]['distance'] for link in aerial_channels])

            Cn2_horizontal=cn2.hufnagel_valley(1e3*float(self.get_config('atmospheric_parameters','balloon_height')),float(self.get_config('atmospheric_parameters','avg_wind_v')),float(self.get_config('atmospheric_parameters','cn0')))
            horchannel = HorizontalChannel(W0=float(self.get_config('atmospheric_parameters','W0_balloon')), 
                                                        rx_aperture=float(self.get_config('atmospheric_parameters','Drx_balloon')), 
                                                        obs_ratio=float(self.get_config('atmospheric_parameters','obstruction_ratio')),
                                                        Cn2=Cn2_horizontal,
                                                        wavelength=1e-9*float(self.get_config('atmospheric_parameters','wavelength')), #converted to m
                                                        pointing_error=float(self.get_config('atmospheric_parameters','pointing_error')),
                                                        tracking_efficiency=float(self.get_config('atmospheric_parameters','tracking_efficiency')),
                                                        )
            horizontal_errors=horchannel._compute_loss_probability((float(horizontal_distance)),n_samples=(int(1e3)))
        if 'DownlinkChannel' in available_links and counter_2 == 0:
            counter_2 += 1     
            downchannel = DownlinkChannel(W0=float(self.get_config('atmospheric_parameters','W0_balloon')), #converted to m
                                                            rx_aperture=float(self.get_config('atmospheric_parameters','Drx_ground')), #converted to m
                                                            obs_ratio=float(self.get_config('atmospheric_parameters','obstruction_ratio')),
                                                            n_max=float(self.get_config('atmospheric_parameters','N_max')),
                                                            Cn0=float(self.get_config('atmospheric_parameters','cn0')),
                                                            wind_speed=float(self.get_config('atmospheric_parameters','avg_wind_v')),
                                                            wavelength=1e-9*float(self.get_config('atmospheric_parameters','wavelength')), #converted to m
                                                            ground_station_alt=1e-3*float(self.get_config('atmospheric_parameters','ground_station_altitude')), #converted to km
                                                            aerial_platform_alt=float(self.get_config('atmospheric_parameters','balloon_height')),
                                                            zenith_angle=float(self.get_config('atmospheric_parameters','zenith_angle')),
                                                            pointing_error=float(self.get_config('atmospheric_parameters','pointing_error')),
                                                            tracking_efficiency=float(self.get_config('atmospheric_parameters','tracking_efficiency')),
                                                            Tatm=float(self.get_config('atmospheric_parameters','T_atm')),
                                                            integral_gain=float(self.get_config('atmospheric_parameters','integral_gain')),
                                                            control_delay=float(self.get_config('atmospheric_parameters','control_delay')),
                                                            integration_time=float(self.get_config('atmospheric_parameters','integration_time')),
                                                            )
            downlink_errors=downchannel._compute_loss_probability((float(self.get_config('atmospheric_parameters','balloon_height'))-1e-3*float(self.get_config('atmospheric_parameters','ground_station_altitude'))),n_samples=(int(1e3)))
        if 'UplinkChannel' in available_links and counter_3 == 0:
            counter_3 += 1    
            upchannel = UplinkChannel(D_tx=float(self.get_config('atmospheric_parameters','W0_ground')),
                                                            R_rx=float(self.get_config('atmospheric_parameters','Drx_balloon')), 
                                                            obs_ratio=float(self.get_config('atmospheric_parameters','obstruction_ratio')),
                                                            n_max=float(self.get_config('atmospheric_parameters','N_max')),
                                                            Cn0=float(self.get_config('atmospheric_parameters','cn0')),
                                                            wind_speed=float(self.get_config('atmospheric_parameters','avg_wind_v')),
                                                            wavelength=1e-9*float(self.get_config('atmospheric_parameters','wavelength')), #converted to m
                                                            ground_station_alt=1e-3*float(self.get_config('atmospheric_parameters','ground_station_altitude')), #converted to km
                                                            aerial_platform_alt=float(self.get_config('atmospheric_parameters','balloon_height')),
                                                            zenith_angle=float(self.get_config('atmospheric_parameters','zenith_angle')),
                                                            pointing_error=float(self.get_config('atmospheric_parameters','pointing_error')),
                                                            tracking_efficiency=float(self.get_config('atmospheric_parameters','tracking_efficiency')),
                                                            Tatm=float(self.get_config('atmospheric_parameters','T_atm')),
                                                            integral_gain=float(self.get_config('atmospheric_parameters','integral_gain')),
                                                            control_delay=float(self.get_config('atmospheric_parameters','control_delay')),
                                                            integration_time=float(self.get_config('atmospheric_parameters','integration_time')),
                                                            )
            uplink_errors=upchannel._compute_loss_probability((float(self.get_config('atmospheric_parameters','balloon_height'))-1e-3*float(self.get_config('atmospheric_parameters','ground_station_altitude'))),n_samples=(int(1e3)))

        #links creation
        for link in self._config['links']:
            link_name = list(link.keys())[0]
            props = list(link.values())[0]
            #store available resources per link
            self._available_links[link_name] = {}
            self._available_links[link_name]['avail'] = props['number_links'] if 'number_links' in props.keys() else 2
            self._available_links[link_name]['occupied'] = []

            nodeA = self.network.get_node(props['end1'])
            nodeB = self.network.get_node(props['end2'])
            # Add Quantum Sources to nodes
            num_qsource = props['number_links'] if 'number_links' in props.keys() else 2
            epr_state = ks.b00 if self._config['epr_pair'] == 'PHI_PLUS' else ks.b01

            state_sampler = StateSampler(
                [epr_state, ks.s00, ks.s01, ks.s10, ks.s11],
                [props['source_fidelity_sq'], (1 - props['source_fidelity_sq'])/4, (1 - props['source_fidelity_sq'])/4,
                 (1 - props['source_fidelity_sq'])/4, (1 - props['source_fidelity_sq'])/4])
            for index_qsource in range(num_qsource):
                if self.get_config('nodes',props['end1'],'type') == 'switch':
                    qsource_origin = nodeA 
                    qsource_dest = nodeB
                else:
                    qsource_origin = nodeB
                    qsource_dest = nodeA
                #Setup QSource
                source_delay = 0 if 'source_delay' not in props.keys() else float(props['source_delay'])
                source = QSource(
                        f"qsource_{qsource_origin.name}_{link_name}_{index_qsource}", state_sampler=state_sampler, num_ports=2, status=SourceStatus.EXTERNAL,
                        models={"emission_delay_model": FixedDelayModel(delay=source_delay)})
                qsource_origin.add_subcomponent(source)
                # Setup Quantum Channels
                #get channel noise model from config
                if self.get_config('links',link_name,'qchannel_noise_model') == 'FibreDepolarizeModel':
                    qchannel_noise_model = FibreDepolarizeModel(p_depol_init=float(self.get_config('links',link_name,'p_depol_init')),
                                                                p_depol_length=float(self.get_config('links',link_name,'p_depol_length')))
                elif self.get_config('links',link_name,'qchannel_noise_model') == 'DephaseNoiseModel':
                    qchannel_noise_model = DephaseNoiseModel(float(self.get_config('links',link_name,'dephase_qchannel_rate')))
                elif self.get_config('links',link_name,'qchannel_noise_model') == 'DepolarNoiseModel':
                    qchannel_noise_model = DepolarNoiseModel(float(self.get_config('links',link_name,'depolar_qchannel_rate')))
                elif self.get_config('links',link_name,'qchannel_noise_model') == 'T1T2NoiseModel':
                    qchannel_noise_model = T1T2NoiseModel(T1=float(self.get_config('links',link_name,'t1_qchannel_time')),
                                              T2=float(self.get_config('links',link_name,'t2_qchannel_time')))
                elif self.get_config('links',link_name,'qchannel_noise_model') == 'FibreDepolGaussModel':
                    qchannel_noise_model = FibreDepolGaussModel()
                else:
                    qchannel_noise_model = None
                
                if self.get_config('links',link_name,'qchannel_loss_model') == 'FibreLossModel':
                    qchannel_loss_model = FibreLossModel(p_loss_init=float(self.get_config('links',link_name,'p_loss_init')),
                                                           p_loss_length=float(self.get_config('links',link_name,'p_loss_length')))
                elif self.get_config('links',link_name,'qchannel_loss_model') == 'DownlinkChannel':
                    qchannel_loss_model=CachedChannel(downlink_errors)
                elif self.get_config('links',link_name,'qchannel_loss_model') == 'UplinkChannel':
                    qchannel_loss_model=CachedChannel(uplink_errors)    
                elif self.get_config('links',link_name,'qchannel_loss_model') == 'AerialHorizontalChannel':
                    qchannel_loss_model=CachedChannel(horizontal_errors)    
                elif self.get_config('links',link_name,'qchannel_loss_model') == 'FreeSpaceLossModel':
                    qchannel_loss_model = FreeSpaceLossModel()                   
                elif self.get_config('links',link_name,'qchannel_loss_model') == 'FixedSatelliteLossModel':
                    qchannel_loss_model = FixedSatelliteLossModel()
                else:
                    qchannel_loss_model = None

                qchannel = QuantumChannel(f"qchannel_{qsource_origin.name}_{qsource_dest.name}_{link_name}_{index_qsource}", 
                        length = props['distance'],
                        models={"quantum_noise_model": qchannel_noise_model, 
                                "quantum_loss_model": qchannel_loss_model,
                                "delay_model": FibreDelayModel(c=float(props['photon_speed_fibre']))})
                port_name_a, port_name_b = self.network.add_connection(
                        qsource_origin, qsource_dest, channel_to=qchannel, 
                        label=f"qconn_{qsource_origin.name}_{qsource_dest.name}_{link_name}_{index_qsource}")

                #Setup quantum ports
                qsource_origin.subcomponents[f"qsource_{qsource_origin.name}_{link_name}_{index_qsource}"].ports["qout1"].forward_output(
                    qsource_origin.ports[port_name_a])
                qsource_origin.subcomponents[f"qsource_{qsource_origin.name}_{link_name}_{index_qsource}"].ports["qout0"].connect(
                    qsource_origin.qmemory.ports[f"qin{self.get_mem_position(qsource_origin.name,link_name,index_qsource)}"])
                qsource_dest.ports[port_name_b].forward_input(
                    qsource_dest.qmemory.ports[f"qin{self.get_mem_position(qsource_dest.name,link_name,index_qsource)}"])
                
                # Setup Classical connections: To be done in routing preparation, depends on paths

    def _measure_link_fidelity(self):
        '''
        Performs a simulation in order to estimate fidelity of each link.
        All links between the same two elements are supossed to have the same fidelity, so only one of them
        is measured in the simulation.
        Input: 
            - will work with self._config
        Output: 
            - will store links with fidelities in self._link_fidelities
        '''
        fidelity_values = []
        for link in self._config['links']:
            link_name = list(link.keys())[0]
            props_link = list(link.values())[0]
            origin = self.network.get_node(props_link['end1'])
            dest = self.network.get_node(props_link['end2'])

            protocol = LinkFidelityProtocol(self,origin,dest,link_name,0,self._config['link_fidel_rounds'])
            protocol.start()
            #runtime = props_link['distance']*float(props_link['photon_speed_fibre'])*25
            #will run as many times as specified in config file
            ns.sim_run()
            #We want to minimize the product of the costs, not the sum. log(ab)=log(a)+log(b)
            #so we will work with logarithm
            self._link_fidelities[list(link.keys())[0]]= [-np.log(np.mean(protocol.fidelities)),np.mean(protocol.fidelities),len(protocol.fidelities)]
            ns.sim_stop()
            ns.sim_reset()
            self._create_network() # Network must be recreated for the simulations to work
    
    def _release_path_resources(self, path):
        '''
        Removes classical connections used by a path and releases quantum links for that path
        Input:
            - path: dict. Path dictionary describing calculated path from origin to destination
        '''
        for nodepos in range(len(path['nodes'])-1):
            nodeA = self.network.get_node(path['nodes'][nodepos])
            nodeB = self.network.get_node(path['nodes'][nodepos+1])
            #Delete classical connections
            for i in [1,2]:
                conn = self.network.get_connection(nodeA, nodeB,f"cconn_{nodeA.name}_{nodeB.name}_{path['request']}_{i}")
                self.network.remove_connection(conn)
                
                conn = self.network.get_connection(nodeA, nodeB,f"cconn_{nodeA.name}_{nodeB.name}_loss_{path['request']}_{i}")
                self.network.remove_connection(conn)
                
                conn = self.network.get_connection(nodeB, nodeA,f"cconn_{nodeB.name}_{nodeA.name}_loss_{path['request']}_{i}")
                self.network.remove_connection(conn)
                #Unable to delete ports. Will remain unconnected

        #remove classical purification connection
        connA = self.network.get_connection(self.network.get_node(path['nodes'][0]), 
                self.network.get_node(path['nodes'][-1]),
                f"cconn_distil_{path['nodes'][0]}_{path['nodes'][-1]}_{path['request']}")
        #Even though classical is bidirectional, only one has to be deleted
        self.network.remove_connection(connA)

        #release quantum channels used by this path
        for link in path['comms']:
            for link_instance in link['links']:
                self.release_link(link_instance.split('-')[0],link_instance.split('-')[1])       

    def _calculate_paths(self):
        first = 1
        for request in self._config['requests']:
            request_name = list(request.keys())[0]
            request_props = list(request.values())[0]

            # Create network graph using available links
            self._graph = nx.Graph()
            for node in self._config['nodes']:
                node_name = list(node.keys())[0]
                node_props = list(node.values())[0]
                if node_props['type'] =='switch':
                    self._graph.add_node(node_name,color='#CF9239',style='filled',fillcolor='#CF9239')
                else:
                    self._graph.add_node(node_name,color='#5DABAB',style='filled',fillcolor='#5DABAB',shape='square')

            for link in self._config['links']:
                link_name = list(link.keys())[0]
                link_props = list(link.values())[0]
                if self._available_links[link_name]['avail']>0:
                    self._graph.add_edge(link_props['end1'],link_props['end2'],weight=self._link_fidelities[link_name][0])

            #Network graph generation, to include in report. Only generated in first iteration
            if first:               
                gr = nx.nx_agraph.to_agraph(self._graph)
                gr.draw('./output/graf.png', prog='fdp')
                first = 0
            
            try:
                shortest_path = nx.shortest_path(self._graph,source=request_props['origin'],target=request_props['destination'], weight='weight')
                purif_rounds = 0
                path = {
                    'request': request_name, 
                    'nodes': shortest_path, 
                    'purif_rounds': purif_rounds,
                    'comms': []}
                for nodepos in range(len(shortest_path)-1):
                    #Get link connecting nodes
                    link = self.get_link(shortest_path[nodepos],shortest_path[nodepos+1],next_index=True)
                    #Determine which of the 2 nodes connected by the link is the source
                    source = shortest_path[nodepos] \
                        if f"qsource_{shortest_path[nodepos]}_{link[0]}_{link[1]}" \
                            in (dict(self.network.get_node(shortest_path[nodepos]).subcomponents)).keys() \
                                else shortest_path[nodepos+1]
                    #Add quantum link to path
                    path['comms'].append({'links': [link[0] + '-' + str(link[1])], 'source': source})

                    #Get classical channel delay model
                    classical_delay_model = None
                    fibre_delay_model = self.get_config('links',link[0], 'classical_delay_model')
                    if fibre_delay_model == 'NOT_FOUND' or fibre_delay_model == 'FibreDelayModel':
                        classical_delay_model = FibreDelayModel(c=float(self.get_config('links',link[0],'photon_speed_fibre')))
                    elif fibre_delay_model == 'GaussianDelayModel':
                        classical_delay_model = GaussianDelayModel(delay_mean=float(self.get_config('links',link[0],'gaussian_delay_mean')),
                                                                        delay_std = float(self.get_config('links',link[0],'gaussian_delay_std')))
                    else: # In case other, we assume FibreDelayModel
                        classical_delay_model = FibreDelayModel(c=float(self.get_config('links',link[0],'photon_speed_fibre')))

                    #Create classical connections for bell measurements. We create channels even if purification is not needed
                    for i in [1,2]:
                        cconn = ClassicalConnection(name=f"cconn_{shortest_path[nodepos]}_{shortest_path[nodepos+1]}_{request_name}_{i}", 
                                                    length=self.get_config('links',link[0],'distance'))
                        cconn.subcomponents['Channel_A2B'].models['delay_model'] = classical_delay_model

                        port_name, port_r_name = self.network.add_connection(
                            self.network.get_node(shortest_path[nodepos]), 
                            self.network.get_node(shortest_path[nodepos+1]), 
                            connection=cconn, label=f"cconn_{shortest_path[nodepos]}_{shortest_path[nodepos+1]}_{request_name}_{i}",
                            port_name_node1=f"ccon_R_{shortest_path[nodepos]}_{request_name}_{i}", 
                            port_name_node2=f"ccon_L_{shortest_path[nodepos+1]}_{request_name}_{i}")

                        #Forward cconn to right most node
                        if f"ccon_L_{path['nodes'][nodepos]}_{request_name}_{i}" in self.network.get_node(path['nodes'][nodepos]).ports:
                            self.network.get_node(path['nodes'][nodepos]).ports[f"ccon_L_{path['nodes'][nodepos]}_{request_name}_{i}"].bind_input_handler(self._handle_message,tag_meta=True)

                    if self.get_config('loss_strategy','loss_strategy') == 'link':
                        #In case 'link' is selected as loss strategy, create classical connections
                        #Communication with next node
                        for i in [1,2]:
                            cconn = ClassicalConnection(name=f"cconn_{shortest_path[nodepos]}_{shortest_path[nodepos+1]}_loss_{request_name}_{i}", 
                                                        length=self.get_config('links',link[0],'distance'))
                            cconn.subcomponents['Channel_A2B'].models['delay_model'] = classical_delay_model

                            port_name, port_r_name = self.network.add_connection(
                                self.network.get_node(shortest_path[nodepos]), 
                                self.network.get_node(shortest_path[nodepos+1]), 
                                connection=cconn, label=f"cconn_{shortest_path[nodepos]}_{shortest_path[nodepos+1]}_loss_{request_name}_{i}",
                                port_name_node1=f"ccon_R_{shortest_path[nodepos]}_{shortest_path[nodepos+1]}_loss_{request_name}_{i}", 
                                port_name_node2=f"ccon_L_{shortest_path[nodepos]}_{shortest_path[nodepos+1]}_loss_{request_name}_{i}")

                        #Communication with previous node
                        for i in [1,2]:
                            cconn = ClassicalConnection(name=f"cconn_{shortest_path[nodepos+1]}_{shortest_path[nodepos]}_loss_{request_name}_{i}", 
                                                        length=self.get_config('links',link[0],'distance'))
                            cconn.subcomponents['Channel_A2B'].models['delay_model'] = classical_delay_model

                            port_name, port_r_name = self.network.add_connection(
                                self.network.get_node(shortest_path[nodepos+1]), 
                                self.network.get_node(shortest_path[nodepos]), 
                                connection=cconn, label=f"cconn_{shortest_path[nodepos+1]}_{shortest_path[nodepos]}_loss_{request_name}_{i}",
                                port_name_node1=f"ccon_L_{shortest_path[nodepos+1]}_{shortest_path[nodepos]}_loss_{request_name}_{i}", 
                                port_name_node2=f"ccon_R_{shortest_path[nodepos+1]}_{shortest_path[nodepos]}_loss_{request_name}_{i}")

                    
                #Setup classical channel for purification
                #calculate distance from first to last node
                total_distance = 0
                average_photon_speed = 0
                for comm in path['comms']:
                    link_distance = self.get_config('links',comm['links'][0].split('-')[0],'distance')
                    link_photon_speed = float(self.get_config('links',comm['links'][0].split('-')[0],'photon_speed_fibre'))
                    total_distance += link_distance
                    average_photon_speed += link_photon_speed * link_distance
                average_photon_speed = average_photon_speed / total_distance


                conn_purif = DirectConnection(
                    f"cconn_distil_{request_name}",
                    ClassicalChannel(f"cconn_distil_{shortest_path[0]}_{shortest_path[-1]}_{request_name}", 
                                     length=total_distance,
                                     models={'delay_model': FibreDelayModel(c=average_photon_speed)}),
                    ClassicalChannel(f"cconn_distil_{shortest_path[-1]}_{shortest_path[0]}_{request_name}", 
                                     length=total_distance,
                                     models={'delay_model': FibreDelayModel(c=average_photon_speed)})
                )
                self.network.add_connection(self.network.get_node(shortest_path[0]), 
                                           self.network.get_node(shortest_path[-1]), connection=conn_purif,
                                           label=f"cconn_distil_{shortest_path[0]}_{shortest_path[-1]}_{request_name}",
                                           port_name_node1=f"ccon_distil_{shortest_path[0]}_{request_name}",
                                           port_name_node2=f"ccon_distil_{shortest_path[-1]}_{request_name}")
                end_simul = False

                #get measurements to do for average fidelity
                fidel_rounds = request_props['path_fidel_rounds'] \
                    if 'path_fidel_rounds' in request_props.keys() else self._config['path_fidel_rounds']

                #Initially no purification
                protocol = PathFidelityProtocol(self,path,fidel_rounds, purif_rounds) #We measure E2E fidelity accordingly to config file times

                while end_simul == False:
                    dc = dc_setup(protocol)
                    protocol.start()
                    ns.sim_run()
                    protocol.stop()
                    
                    print(f"Request {request_name} purification rounds {purif_rounds} fidelity {dc.dataframe['Fidelity'].mean()}/{request_props['minfidelity']} in {dc.dataframe['time'].mean()}/{request_props['maxtime']} nanoseconds, data points: {len(dc.dataframe)}")
                    if dc.dataframe["time"].mean() > request_props['maxtime']:
                        #request cannot be fulfilled. Mark as rejected and continue
                        self._requests_status.append({
                            'request': request_name, 
                            'shortest_path': shortest_path,
                            'result': 'rejected', 
                            'reason': 'cannot fulfill time',
                            'purif_rounds': purif_rounds,
                            'fidelity': dc.dataframe["Fidelity"].mean(),
                            'time': dc.dataframe["time"].mean()})
                        
                        #release classical and quantum channels
                        self._release_path_resources(path)

                        end_simul = True
                    elif dc.dataframe["Fidelity"].mean() >= request_props['minfidelity']:
                        #request can be fulfilled
                        self._requests_status.append({
                            'request': request_name, 
                            'shortest_path': shortest_path,
                            'result': 'accepted', 
                            'reason': '-',
                            'purif_rounds': purif_rounds,
                            'fidelity': dc.dataframe["Fidelity"].mean(),
                            'time': dc.dataframe["time"].mean()})
                        path['purif_rounds'] = purif_rounds
                        self._paths.append(path)
                        end_simul=True
                    else: #purification is needed
                        purif_rounds += 1
                        #if first time with purification add second quantum link in path
                        if purif_rounds == 1:
                            #check if we have available link resources for second path
                            available_resources = True
                            for comm in path['comms']:
                                link_name = comm['links'][0].split('-')[0]
                                if self._available_links[link_name]['avail'] == 0:
                                    available_resources = False
                                    break

                            if not available_resources:
                                #No available resources for second link instance, must free path resources
                                self._release_path_resources(path)

                                #return no path
                                shortest_path = 'NOPATH'
                                self._requests_status.append({
                                    'request': request_name, 
                                    'shortest_path': shortest_path,
                                    'result': 'rejected', 
                                    'reason': 'no available resources',
                                    'purif_rounds': 'na',
                                    'fidelity': 0,
                                    'time': 0})
                                
                                end_simul = True

                            else:
                                new_comms = []
                                for nodepos in range(len(shortest_path)-1):
                                    link = self.get_link(shortest_path[nodepos],shortest_path[nodepos+1],next_index=True)
                                    for comm in path['comms']:
                                        if comm['links'][0].split('-')[0] == link[0]:
                                            comm['links'].append(link[0] + '-' + str(link[1]))
                                            new_comms.append(comm)
                                path['comms'] = new_comms   
                                protocol.set_purif_rounds(purif_rounds)

                        else:
                            protocol.set_purif_rounds(purif_rounds)

            except nx.exception.NetworkXNoPath:
                shortest_path = 'NOPATH'
                self._requests_status.append({
                            'request': request_name, 
                            'shortest_path': shortest_path,
                            'result': 'rejected', 
                            'reason': 'no available resources',
                            'purif_rounds': '-',
                            'fidelity': 0,
                            'time': 0})

    def _handle_message(self,msg):
        input_port = msg.meta['rx_port_name']
        forward_port = input_port.replace('ccon_L_','ccon_R_')
        port_elements = input_port.split('_')
        node = self.network.get_node(port_elements[2])
        node.ports[forward_port].tx_output(msg)
        return

    def _create_qprocessor(self,name,num_memories,nodename):
        '''
        Factory to create a quantum processor for each node.

        In an end node it has 4 memory positions. In a swich 2xnum_links.
        Adapted from available example in NetSquid website

        Input:
            - name: name of quantum processor
            - nodename: name of node where it is placed

        Output:
            - instance of QuantumProcessor

        '''

        _INSTR_Rx = IGate("Rx_gate", ops.create_rotation_op(np.pi / 2, (1, 0, 0)))
        _INSTR_RxC = IGate("RxC_gate", ops.create_rotation_op(np.pi / 2, (1, 0, 0), conjugate=True))

        #get gate durations from configuration
        gate_duration = self.get_config('nodes',nodename,'gate_duration') \
            if self.get_config('nodes',nodename,'gate_duration') != 'NOT_FOUND' else 0
        gate_duration_X = self.get_config('nodes',nodename,'gate_duration_X') \
            if self.get_config('nodes',nodename,'gate_duration_X') != 'NOT_FOUND' else gate_duration
        gate_duration_Z = self.get_config('nodes',nodename,'gate_duration_Z') \
            if self.get_config('nodes',nodename,'gate_duration_Z') != 'NOT_FOUND' else gate_duration
        gate_duration_CX = self.get_config('nodes',nodename,'gate_duration_CX') \
            if self.get_config('nodes',nodename,'gate_duration_CX') != 'NOT_FOUND' else gate_duration
        gate_duration_rotations = self.get_config('nodes',nodename,'gate_duration_rotations') \
            if self.get_config('nodes',nodename,'gate_duration_rotations') != 'NOT_FOUND' else gate_duration
        measurements_duration = self.get_config('nodes',nodename,'measurements_duration') \
            if self.get_config('nodes',nodename,'measurements_duration') != 'NOT_FOUND' else gate_duration

        #get gate noise model
        if self.get_config('nodes',nodename,'gate_noise_model') == 'DephaseNoiseModel':
            gate_noise_model = DephaseNoiseModel(float(self.get_config('nodes',nodename,'dephase_gate_rate')))
        elif self.get_config('nodes',nodename,'gate_noise_model') == 'DepolarNoiseModel':
            gate_noise_model = DepolarNoiseModel(float(self.get_config('nodes',nodename,'depolar_gate_rate')))
        elif self.get_config('nodes',nodename,'gate_noise_model') == 'T1T2NoiseModel':
            gate_noise_model = T1T2NoiseModel(T1=float(self.get_config('nodes',nodename,'t1_gate_time')),
                                              T2=float(self.get_config('nodes',nodename,'t2_gate_time')))
        else:
            gate_noise_model = None

        #set memories noise model
        if self.get_config('nodes',nodename,'mem_noise_model') == 'DephaseNoiseModel':
            mem_noise_model = DephaseNoiseModel(float(self.get_config('nodes',nodename,'dephase_mem_rate')))
        elif self.get_config('nodes',nodename,'mem_noise_model') == 'DepolarNoiseModel':
            mem_noise_model = DepolarNoiseModel(float(self.get_config('nodes',nodename,'depolar_mem_rate')))
        elif self.get_config('nodes',nodename,'mem_noise_model') == 'T1T2NoiseModel':
            mem_noise_model = T1T2NoiseModel(T1=float(self.get_config('nodes',nodename,'t1_mem_time')),
                                              T2=float(self.get_config('nodes',nodename,'t2_mem_time')))
        else:
            mem_noise_model = None

        #define available instructions   
        physical_instructions = [
            PhysicalInstruction(INSTR_X, duration=gate_duration_X,
                                quantum_noise_model=gate_noise_model
                                ),
            PhysicalInstruction(INSTR_Z, duration=gate_duration_Z,
                                quantum_noise_model=gate_noise_model
                                ),
            PhysicalInstruction(INSTR_MEASURE_BELL, 
                                duration=(measurements_duration+gate_duration_CX+gate_duration),
                                quantum_noise_model=gate_noise_model),
            PhysicalInstruction(INSTR_MEASURE, 
                                duration=measurements_duration,
                                quantum_noise_model=gate_noise_model),
            PhysicalInstruction(INSTR_CNOT, 
                                duration=gate_duration_CX,
                                quantum_noise_model=gate_noise_model),
            PhysicalInstruction(_INSTR_Rx, 
                                duration=gate_duration_rotations,
                                quantum_noise_model=gate_noise_model),
            PhysicalInstruction(_INSTR_RxC, 
                                duration=gate_duration_rotations,
                                quantum_noise_model=gate_noise_model),
            PhysicalInstruction(INSTR_CCX, 
                                duration=gate_duration_CX,
                                quantum_noise_model=gate_noise_model),
            PhysicalInstruction(INSTR_H, 
                                duration=gate_duration_X,
                                quantum_noise_model=gate_noise_model)
        ]
        #nvproc = NVQuantumProcessor(name, num_positions=num_memories)
        #build quantum processor
        qproc = QuantumProcessor(name, 
                                 num_positions=num_memories, 
                                 phys_instructions = physical_instructions,
                                 fallback_to_nonphysical=False,
                                 mem_noise_models=[mem_noise_model] * num_memories)
        return qproc

def compute_channel_length(ground_station_alt, aerial_platform_alt, zenith_angle):
    """Compute channel length that corresponds to a particular ground station altitude, aerial 
    platform altitude and zenith angle.

    ## Parameters
        `ground_station_alt` : float 
            Altitude of the ground station [km].
        `aerial_platform_alt` : float 
            Altitude of the aerial platform [km].
        `zenith_angle` : float
            Zenith angle of aerial platform [degrees].
    ## Returns
    `length` : float
        Length of the channel [km].
    """
    zenith_angle = np.deg2rad(zenith_angle)
    RA = RE + aerial_platform_alt
    RG = RE + ground_station_alt
    length = np.sqrt(RA**2 + RG**2*(np.cos(zenith_angle)**2 - 1)) - RG*np.cos(zenith_angle)
    return length

def compute_height_min_horiz (length, height):
    """Compute minimal height of a horizontal channel between two ballons at the same height.
    
    ## Parameters 
        `length` : float
            length of the horizontal channel [km]
        `height̀` : float
            height of the balloons [km]
    ## Returns 
    `hmin` : float
        Minimal height of the channel [km] 
    """
    RS = RE + height
    theta = np.arcsin((length)/(2*RS))
    hmin = np.cos(theta)*RS -RE
    return hmin
    
def sec(theta):
    """Compute secant of angle theta.

    ## Parameters
    `theta` : float
        Angle for which secant will be calculated [degrees].
    ## Returns
    `sec` : float
        Secant result.
    """
    theta = np.deg2rad(theta)
    sec = 1/np.cos(theta)
    return sec

def lognormal_pdf(eta, mu, sigma):
    """Compute lognormal distribution probability density function (PDF).

    ## Parameters
    `eta` : np.ndarray
        Input random variable values to calculate PDF for.
    `mu` : float
        Mean value of lognormal distribution.
    `sigma` : float
        Standard deviation of lognormal distribution.
    ## Returns
    `pdf` : np.ndarray
        PDF of lognormal distribution for values of eta.
    """

    pdf = np.exp(-(np.log(eta) + mu)**2/(2*sigma**2))/(eta*sigma*np.sqrt(2*np.pi))
    return pdf

def lognormal_cdf(eta, mu, sigma):
    """Compute lognormal distribution cumulative density function (CDF).

    ## Parameters
    `eta` : np.ndarray
        Input random variable values to calculate CDF for.
    `mu` : float
        Mean value of lognormal distribution.
    `sigma` : float
        Standard deviation of lognormal distribution.
    ## Returns
    `cdf` : np.ndarray
        CDF of lognormal distribution for values of eta.
    """
    cdf = (1 + erf((np.log(eta) + mu)/(sigma*np.sqrt(2))))/2
    return cdf

def truncated_lognormal_pdf(eta, mu, sigma):
    """Compute truncated lognormal distribution probability density function (PDF) according to [Vasylyev et al., 2018].

    ## Parameters
    `eta` : np.ndarray
        Input random variable values to calculate PDF for.
    `mu` : float
        Mean value of truncated lognormal distribution.
    `sigma` : float
        Standard deviation of truncated lognormal distribution.
    ## Returns
    `pdf` : np.ndarray
        PDF of truncated lognormal distribution for values of eta.
    """
    lognormal_cdf_dif = lognormal_cdf(1, mu, sigma)
    if np.size(eta) == 1:
        if eta < 0 or eta > 1:
            pdf = 0
        else: 
            pdf = lognormal_pdf(eta, mu, sigma)/lognormal_cdf_dif
    else:
        pdf = np.zeros(np.size(eta))
        eta_domain = (eta >= 0) & (eta <= 1)
        pdf = lognormal_pdf(eta[eta_domain], mu, sigma)/lognormal_cdf_dif
    return pdf

def lognegative_weibull_pdf(eta, eta_0, wandering_variance, R, l):
    """Compute log-negative Weiibull distribution probability density function (PDF) according to [Vasylyev et al., 2018].

    ## Parameters
    `eta` : np.ndarray
        Input random variable values to calculate PDF for.
    `eta_0` : float
        Maximal transmittance of the Gaussian beam.
    `wandering_variance` : float
        Wandering variance of the Gaussian beam.
    `R` : float
        Scale parameter of distribution.
    `l` : float
        Shape parameter of distribution.
    ## Returns
    `pdf` : np.ndarray
        PDF of log-negative Weibull distribution for values of eta.
    """
    if np.size(eta) == 1:
        if eta < 0 or eta > eta_0:
            pdf = 0
        else: 
            pdf = (R**2/(wandering_variance*eta*l))*((np.log(eta_0/eta))**(2/l - 1))*np.exp(-(R**2/(2*wandering_variance))*(np.log(eta_0/eta))**(2/l))
    else:
        pdf = np.zeros(np.size(eta))
        eta_domain = (eta >= 0) & (eta <= eta_0)
        pdf[eta_domain] = (R**2/(wandering_variance*eta[eta_domain]*l))*((np.log(eta_0/eta[eta_domain]))**(2/l - 1))*np.exp(-(R**2/(2*wandering_variance))*(np.log(eta_0/eta[eta_domain]))**(2/l))
    return pdf

class HorizontalChannel(QuantumErrorModel):
    """Model for photon loss on a horizontal free-space channel.

    Uses probability density of atmospheric transmittance (PDT) from [Vasylyev et al., 2018] to
    sample the loss probability of the photon.

    ## Parameters
    ----------
    `W0` : float
        Waist radius of the beam at the transmitter [m].
    `rx_aperture` : float
        Diameter of the receiving telescope [m].
    `obs_ratio` : float
        Obscuration ratio of the receiving telescope.
    `Cn2` : float
        Index of refraction structure constant [m**(-2/3)].
    `wavelength` : float
        Wavelength of the radiation [m].
    `pointing_error` : float
        Pointing error [rad].
    `tracking_efficiency` : float
        Efficiency of the coarse tracking mechanism.
    `Tatm` : float
        Atmospheric transmittance (square of the transmission coefficient).
    `rng` : :obj:`~numpy.random.RandomState` or None, optional
        Random number generator to use. If ``None`` then
        :obj:`~netsquid.util.simtools.get_random_state` is used.
    """
    def __init__(self, W0, rx_aperture, obs_ratio, Cn2, wavelength, pointing_error = 0, tracking_efficiency = 0, Tatm = 1, rng = None):
        super().__init__()
        self.rng = rng if rng else simtools.get_random_state()
        self.W0 = W0
        self.rx_aperture = rx_aperture
        self.obs_ratio = obs_ratio
        self.Cn2 = Cn2
        self.wavelength = wavelength
        self.pointing_error = pointing_error
        self.tracking_efficiency = tracking_efficiency
        self.Tatm = Tatm
        self.required_properties = ['length']

    @property
    def rng(self):
        """ :obj:`~numpy.random.RandomState`: Random number generator."""
        return self.properties['rng']

    @rng.setter
    def rng(self, value):
        if not isinstance(value, np.random.RandomState):
            raise TypeError("{} is not a valid numpy RandomState".format(value))
        self.properties['rng'] = value
        
    @property
    def Tatm(self):
        """ :float: atmosphere transmittance. """
        return self.properties['Tatm']

    @Tatm.setter
    def Tatm(self, value):
        if (value < 0) or (value > 1):
            raise ValueError
        self.properties['Tatm'] = value

    @property
    def pointing_error(self):
        """ :float: pointing error variance. """
        return self.properties['pointing_error']

    @pointing_error.setter
    def pointing_error(self, value):
        if (value < 0):
            raise ValueError
        self.properties['pointing_error'] = value

    @property
    def tracking_efficiency(self):
        """ :float: efficiency of the coarse tracking mechanism. """
        return self.properties['tracking_efficiency']

    @tracking_efficiency.setter
    def tracking_efficiency(self, value):
        if (value < 0) or (value > 1):
            raise ValueError
        self.properties['tracking_efficiency'] = value

    @property
    def W0(self):
        """float: beam waist radius at the transmitter [m]."""
        return self.properties['W0']

    @W0.setter
    def W0(self, value):
        if value < 0:
            raise ValueError
        self.properties['W0'] = value

    @property
    def rx_aperture(self):
        """float: diameter of the receiving telescope [m]."""
        return self.properties['rx_aperture']

    @rx_aperture.setter
    def rx_aperture(self, value):
        if value < 0:
            raise ValueError
        self.properties['rx_aperture'] = value

    @property
    def obs_ratio(self):
        """float: obscuration ratio of the receiving telescope."""
        return self.properties['obs_ratio']

    @obs_ratio.setter
    def obs_ratio(self, value):
        if value < 0 or (value > 1):
            raise ValueError
        self.properties['obs_ratio'] = value

    @property
    def Cn2(self):
        """float: index of refraction structure constant [m**(-2/3)]."""
        return self.properties['Cn2']

    @Cn2.setter
    def Cn2(self, value):
        if value < 0:
            raise ValueError
        self.properties['Cn2'] = value

    @property
    def wavelength(self):
        """float: wavelength of the radiation [m]."""
        return self.properties['wavelength']

    @wavelength.setter
    def wavelength(self, value):
        if value < 0:
            raise ValueError
        self.properties['wavelength'] = value

    def _compute_rytov_variance(self, length):
        """Compute rytov variance for a horizontal channel [Andrews/Phillips, 2005].

        ## Parameters
        `length` : float 
            Length of the channel [m].
        ## Returns
        `rytov_var` : float
            Rytov variance for given length.
        """
        k = 2*np.pi/self.wavelength
        rytov_var = 1.23*self.Cn2*k**(7/6)*length**(11/6)
        return rytov_var
    
    def _compute_wandering_variance(self, length):
        """Compute beam wandering variance for a horizontal channel [Andrews/Phillips, 2005].

        ## Parameters
        `length` : float 
            Length of the channel [m].
        ## Returns
        `wandering_var` : float
            Beam wandering variance for given length [m^2].
        """
        k = 2*np.pi/self.wavelength
        Lambda_0 = 2*length/(k*self.W0**2)
        Theta_0 = 1
        rytov_var = self._compute_rytov_variance(length)
        f = lambda xi: (Theta_0 + (1 - Theta_0)*xi)**2 + 1.63*(rytov_var)**(6/5)*Lambda_0*(1 - xi)**(16/5)
        integrand = lambda xi: xi**2/f(xi)**(1/6)
        wandering_var = 7.25*self.Cn2*self.W0**(-1/3)*length**3*quad(integrand, 0, 1)[0]
        return wandering_var
    
    def _compute_scintillation_index_plane(self, rytov_var, length):
        """Compute aperture-averaged scintillation index of plane wave for a horizontal channel [Andrews/Phillips, 2005].

        ## Parameters
        `length` : float 
            Length of the channel [m].
        `rytov_var` : float 
            Rytov variance.
        ## Returns
        `scint_index` : float
            Scintillation index for requested input parameters.
        """
        k = 2*np.pi/self.wavelength
        d = np.sqrt(k*self.rx_aperture**2/(4*length))
        first_term = 0.49*rytov_var/(1 + 0.65*d**2 + 1.11*rytov_var**(6/5))**(7/6)
        second_term = 0.51*rytov_var*(1 + 0.69*rytov_var**(6/5))**(-5/6)/(1 + 0.9*d**2 + 0.62*d**2*rytov_var**(6/5))
        scint_index = np.exp(first_term + second_term) - 1
        return scint_index
    
    def _compute_scintillation_index_spherical(self, rytov_var, length):
        """Compute aperture-averaged scintillation index of spherical wave for a horizontal channel [Andrews/Phillips, 2005].

        ## Parameters
        `length` : float 
            Length of the channel [m].
        `rytov_var` : float 
            Rytov variance.
        ## Returns
        `scint_index` : float
            Scintillation index for requested input parameters.
        """
        k = 2*np.pi/self.wavelength
        d = np.sqrt(k*self.rx_aperture**2/(4*length))
        beta_0_sq = 0.4065*rytov_var
        first_term = 0.49*beta_0_sq/(1 + 0.18*d**2 + 0.56*beta_0_sq**(6/5))**(7/6)
        second_term = 0.51*beta_0_sq*(1 + 0.69*beta_0_sq**(6/5))**(-5/6)/(1 + 0.9*d**2 + 0.62*d**2*beta_0_sq**(6/5))
        return np.exp(first_term + second_term) - 1

    def _compute_coherence_width_plane(self, length):
        """Compute coherence width of plane wave for a horizontal channel [Andrews/Phillips, 2005].

        ## Parameters
        `length` : float 
            Length of the channel [m].
        ## Returns
        `coherence_width` : float
            Coherence width for requested input parameters.
        """ 
        k = 2*np.pi/self.wavelength
        coherence_width = (0.42*length*self.Cn2*k**2)**(-3/5) 
        return coherence_width
    
    def _compute_coherence_width_spherical(self, length):
        """Compute coherence width of spherical wave for a horizontal channel [Andrews/Phillips, 2005].

        ## Parameters
        `length` : float 
            Length of the channel [m].
        ## Returns
        `coherence_width` : float
            Coherence width for requested input parameters.
        """ 
        k = 2*np.pi/self.wavelength
        coherence_width = (0.16*length*self.Cn2*k**2)**(-3/5) 
        return coherence_width

    def _compute_coherence_width_gaussian(self, length):
        """Compute coherence width of gaussian wave for a horizontal channel (valid also for the 
        strong tubulence regime) [Andrews/Phillips, 2005].

        ## Parameters
        `length` : float 
            Length of the channel [m].
        ## Returns
        `coherence_width` : float
            Coherence width for requested input parameters.
        """
        k = 2*np.pi/self.wavelength
        Lambda_0 = 2*length/(k*self.W0**2)
        Lambda = Lambda_0/(1 + Lambda_0**2)
        Theta = 1/(1 + Lambda_0**2)
        rho_plane = (1.46*self.Cn2*length*k**2)**(-3/5)
        q = length/(k*rho_plane**2)
        Theta_e = (Theta - 2*q*Lambda/3)/(1 + 4*q*Lambda/3)
        Lambda_e = Lambda/(1 + 4*q*Lambda/3)
        if Theta_e >= 0:
            a_e = (1 - Theta_e**(8/3))/(1 - Theta_e)
        else:
            a_e = (1 + np.abs(Theta_e)**(8/3))/(1 - Theta_e)
        coherence_width = 2.1*rho_plane*(8/(3*(a_e + 0.618*Lambda_e**(11/6))))**(3/5)
        return coherence_width
    
    def _compute_long_term_beam_size_at_receiver(self, rytov_var, length):
        """Compute long-term beamsize at receiver for a horizontal channel [Andrews/Phillips, 2005].

        ## Parameters
        `length` : float 
            Length of the channel [m].
        `rytov_var` : float 
            Rytov variance.
        ## Returns
        `W_LT` : float
            Long-term beamsize at receiver for requested input parameters [m].
        """
        k = 2*np.pi/self.wavelength
        W_LT = self.W0*np.sqrt(1 + (self.wavelength*length/(np.pi*self.W0**2))**2 + 1.63*rytov_var**(6/5)*2*length/(k*self.W0**2))
        return W_LT
    
    def _compute_short_term_beam_size_at_receiver(self, long_term_beamsize, wandering_var):
        """Compute short-term beamsize at receiver for a horizontal channel [Andrews/Phillips, 2005].

        ## Parameters
        `long_term_beamsize` : float 
            Long-term beamsize at the receiver [m].
        `wandering_var` : float 
            Beam wandering variance at receiver [m^2].
        ## Returns
        `W_ST` : float
            Short-term beamsize at receiver for requested input parameters [m].
        """
        W_ST = np.sqrt(long_term_beamsize**2 - wandering_var)
        return W_ST
    
    def _compute_pdt(self, eta, length):
        """Compute probability distribution of atmospheric transmittance (PDT) [Vasylyev et al., 2018].

        ## Parameters
        `eta` : np.ndarray
            Input random variable values to calculate PDT for.
        `length` : float 
            Length of the channel [km].
        ## Returns
        `integral` : np.ndarray
            PDT function for input eta.
        """
        z = length*1e3
        rx_radius = self.rx_aperture/2
        rytov_var = self._compute_rytov_variance(z)
        pointing_var = (self.pointing_error*z)**2
        wandering_var = (self._compute_wandering_variance(z) + pointing_var)*(1 - self.tracking_efficiency)
        wandering_percent = 100*np.sqrt(wandering_var)/rx_radius
        if wandering_percent > 100:
            print("Warning ! The total wandering is larger than the aperture of the receiver. Use smaller values of pointing error.")

        W_LT = self._compute_long_term_beam_size_at_receiver(rytov_var, z)
        W_ST = self._compute_short_term_beam_size_at_receiver(W_LT, wandering_var)

        X = (rx_radius/W_ST)**2
        T0 = np.sqrt(1 - np.exp(-2*X))
        l = 8 * X * np.exp(-4*X) * i1(4*X) / (1 - np.exp(-4*X)*i0(4*X))/np.log(2*T0**2/(1 - np.exp(-4*X)*i0(4*X)))
        R = rx_radius * np.log(2*T0**2/(1 - np.exp(-4*X)*i0(4*X)))**(-1./l)

        if wandering_var >= 1e-7:
            pdt = lognegative_weibull_pdf(eta, T0, wandering_var, R, l)
        else: 
            pdt = np.zeros(np.size(eta))
            delta_eta = np.abs(eta[1] - eta[0])
            pdt[np.abs(eta - T0)  < delta_eta] = 1
        return pdt
    
    def _compute_channel_pdf(self, eta_ch, length):
        """Compute probability density function (PDF) of free-space channel efficiency.

        ## Parameters
        `eta_ch` : np.ndarray
            Input random variable values to calculate pdf for.
        `length` : float 
            Length of the channel [km].
        ## Returns
        `ch_pdf` : np.ndarray
            Channel PDF for input eta.
        """
        pdt = self._compute_pdt(eta_ch, length)
        pdt = pdt/np.sum(pdt)

        z = length*1e3
        n = lut_zernike_index_pd["n"]
        n = np.array(lut_zernike_index_pd["n"].values)
        rytov_var = self._compute_rytov_variance(z)
        r0 = self._compute_coherence_width_gaussian(z)
        eta_s = (1 + rytov_var)**(-1/4)
        bj2 = smf.bn2(self.rx_aperture, r0, n, self.obs_ratio)
        beta_opt = smf.beta_opt(self.obs_ratio)
        eta_smf_max = smf.eta_0(self.obs_ratio, beta_opt)
        ch_pdf = pdt*self.Tatm*smf.eta_ao(bj2)*eta_s*eta_smf_max
        return ch_pdf
    
    def _compute_mean_channel_efficiency(self, eta_ch, length, detector_efficiency = 1):
        """Compute mean channel efficiency, including losses at the detector.

        ## Parameters
        `eta_ch` : np.ndarray
            Input random variable values to calculate pdf for.
        `length` : float 
            Length of the channel [km].
        `detector_efficiency` : float
            Efficiency of detector at receiver (default 1).
        ## Returns
        `ch_pdf` : np.ndarray
            Channel PDF for input eta.
        """
        pdt = self._compute_pdt(eta_ch, length)
        pdt = pdt/np.sum(pdt)
        z = length*1e3
        n = lut_zernike_index_pd["n"]
        n = np.array(lut_zernike_index_pd["n"].values)
        rytov_var = self._compute_rytov_variance(z)
        r0 = self._compute_coherence_width_gaussian(z)
        eta_s = (1 + rytov_var)**(-1/4)
        bj2 = smf.bn2(self.rx_aperture, r0, n,self.obs_ratio)
        beta_opt = smf.beta_opt(self.obs_ratio)
        eta_smf_max = smf.eta_0(self.obs_ratio, beta_opt)
        mean_transmittance = np.sum(eta_ch*pdt)*self.Tatm*smf.eta_ao(bj2)*eta_s*eta_smf_max*detector_efficiency
        return mean_transmittance
    
    def _draw_pdt_sample(self, length, n_samples):
        """Draw random sample from probability distribution of atmospheric transmittance (PDT).

        ## Parameters
        `length` : float 
            Length of the channel [km].
        `n_samples` : int
            Number of samples to return.
        ## Returns
        `samples` : float
            Random samples of PDT.
        """
        eta = np.linspace(1e-7, 1, 1000)
        pdt = self._compute_pdt(eta, length)
        pdt = np.abs(pdt/np.sum(pdt))
        samples = np.random.choice(eta, n_samples, p = pdt)
        return samples

    def _draw_channel_pdf_sample(self, length, n_samples):
        """Draw random sample from free-space channel probability distribution.
    
        ## Parameters
        `length` : float 
            Length of the channel [km].
        `n_samples` : int
            Number of samples to return.
        ## Returns
        `samples` : float
            Random samples of channel PDF.
        """
        z = length*1e3
        eta = np.linspace(1e-7, 1, 1000)
        ch_pdf = self._compute_channel_pdf(eta, length)
        ch_pdf = np.abs(ch_pdf/np.sum(ch_pdf))
        ch_pdf_samples = np.random.choice(eta, n_samples, p = ch_pdf)
        rytov_var = self._compute_rytov_variance(z)
        scint_index = self._compute_scintillation_index_spherical(rytov_var, z)
        eta_s = (1 + scint_index)**(-1/4)
        beta_opt = smf.beta_opt(self.obs_ratio)
        eta_smf_max = smf.eta_0(self.obs_ratio, beta_opt)

        return self.Tatm*ch_pdf_samples*eta_smf_max*eta_s
    
    def _compute_loss_probability(self, length, n_samples):
        """Compute loss probability of photon in horizontal channel, taking all losses into account.

        ## Parameters
        `length` : float 
            Length of the channel [km].
        `n_samples` : int
            Number of samples to return.
        ## Returns
        `prob_loss` : float
            Probability that a photon is lost in the channel.
        """
        T = self._draw_channel_pdf_sample(length, n_samples)
        prob_loss = 1 - T
        return prob_loss
    
    def error_operation(self, qubits, **kwargs):
        """Error operation to apply to qubits.

        ## Parameters
        qubits : tuple of :obj:`~netsquid.qubits.qubit.Qubit`
            Qubits to apply noise to.
        """
        if 'channel' in kwargs:
            warn_deprecated("channel parameter is deprecated. "
                            "Pass length parameter directly instead.",
                            key="FreeSpaceLossModel.compute_model.channel")
            kwargs['length'] = kwargs['channel'].properties["length"]
            del kwargs['channel']

        prob_loss = self._compute_loss_probability(length = kwargs['length'], n_samples = len(qubits))
        for idx, qubit in enumerate(qubits):
            if qubit is None:
                continue
            self.lose_qubit(qubits, idx, prob_loss[idx], rng = self.properties['rng'])

class DownlinkChannel(QuantumErrorModel):
    """Model for photon loss on a downlink free-space channel.

    Uses probability density of atmospheric transmittance (PDT) from [Vasylyev et al., 2018] to
    sample the loss probability of the photon.

    ## Parameters
    ----------
    `W0` : float
        Waist radius of the beam at the transmitter [m].
    `rx_aperture` : float
        Diameter of the receiving telescope [m].
    `obs_ratio` : float
        Obscuration ratio of the receiving telescope.
    `n_max` : int
        Maximum radial index of correction of AO system.
    `Cn0` : float
        Reference index of refraction structure constant at ground level [m**(-2/3)].
    `wind_speed` : float
        Rms speed of the wind [m/s].  
    `wavelength` : float
        Wavelength of the radiation [m].
    `ground_station_alt` : float 
        Altitude of the ground station [km].
    `aerial_platform_alt` : float 
        Altitude of the aerial platform [km].
    `zenith_angle` : float
        Zenith angle of aerial platform [degrees].
    `pointing_error` : float
        Pointing error [rad].
    `tracking_efficiency` : float
        Efficiency of the coarse tracking mechanism.
    `Tatm` : float
        Atmospheric transmittance (square of the transmission coefficient).
    `integral_gain: float`
        Integral gain of the AO system integral controller.
    `control_delay: float`
        Delay of the AO system loop [s].
    `integration_time: float`
        Integration time of the AO system integral controller [s].
    `rng` : :obj:`~numpy.random.RandomState` or None, optional
        Random number generator to use. If ``None`` then
        :obj:`~netsquid.util.simtools.get_random_state` is used.
    """
    def __init__(self, W0, rx_aperture, obs_ratio, n_max, Cn0, wind_speed, wavelength, 
                 ground_station_alt, aerial_platform_alt, zenith_angle, pointing_error = 0, 
                 tracking_efficiency = 0, Tatm = 1, integral_gain = 1, control_delay = 13.32e-4, integration_time = 6.66e-4, rng = None):
        super().__init__()
        self.rng = rng if rng else simtools.get_random_state()
        self.W0 = W0
        self.rx_aperture = rx_aperture
        self.obs_ratio = obs_ratio
        self.n_max = n_max
        self.Cn2 = Cn0
        self.wind_speed = wind_speed
        self.wavelength = wavelength
        self.ground_station_alt = ground_station_alt
        self.aerial_platform_alt = aerial_platform_alt
        self.zenith_angle = zenith_angle
        self.pointing_error = pointing_error
        self.integral_gain = integral_gain
        self.control_delay = control_delay
        self.integration_time = integration_time
        self.tracking_efficiency = tracking_efficiency
        self.Tatm = Tatm
        self.required_properties = ['length']

    @property
    def rng(self):
        """ :obj:`~numpy.random.RandomState`: Random number generator."""
        return self.properties['rng']

    @rng.setter
    def rng(self, value):
        if not isinstance(value, np.random.RandomState):
            raise TypeError("{} is not a valid numpy RandomState".format(value))
        self.properties['rng'] = value
    
    @property
    def Tatm(self):
        """ :float: atmosphere transmittance. """
        return self.properties['Tatm']

    @Tatm.setter
    def Tatm(self, value):
        if (value < 0) or (value > 1):
            raise ValueError
        self.properties['Tatm'] = value

    @property
    def pointing_error(self):
        """ :float: pointing error variance. """
        return self.properties['pointing_error']

    @pointing_error.setter
    def pointing_error(self, value):
        if (value < 0):
            raise ValueError
        self.properties['pointing_error'] = value

    @property
    def tracking_efficiency(self):
        """ :float: efficiency of the coarse tracking mechanism. """
        return self.properties['tracking_efficiency']

    @tracking_efficiency.setter
    def tracking_efficiency(self, value):
        if (value < 0) or (value > 1):
            raise ValueError
        self.properties['tracking_efficiency'] = value

    @property
    def W0(self):
        """float: beam waist radius at the transmitter [m]."""
        return self.properties['W0']

    @W0.setter
    def W0(self, value):
        if value < 0:
            raise ValueError
        self.properties['W0'] = value

    @property
    def rx_aperture(self):
        """float: diameter of the receiving telescope [m]."""
        return self.properties['rx_aperture']

    @rx_aperture.setter
    def rx_aperture(self, value):
        if value < 0:
            raise ValueError
        self.properties['rx_aperture'] = value

    @property
    def obs_ratio(self):
        """float: obscuration ratio of the receiving telescope."""
        return self.properties['obs_ratio']

    @obs_ratio.setter
    def obs_ratio(self, value):
        if value < 0 or (value > 1):
            raise ValueError
        self.properties['obs_ratio'] = value

    @property
    def n_max(self):
        """float: maximum radial index of correction of AO system."""
        return self.properties['n_max']

    @n_max.setter
    def n_max(self, value):
        if value < 0:
            raise ValueError
        self.properties['n_max'] = value


    @property
    def integral_gain(self):
        """float: integral gain of the AO system integral controller."""
        return self.properties['integral_gain']

    @integral_gain.setter
    def integral_gain(self, value):
        if value < 0:
            raise ValueError
        self.properties['integral_gain'] = value

    @property
    def control_delay(self):
        """float: delay of the AO system loop [s]."""
        return self.properties['control_delay']

    @control_delay.setter
    def control_delay(self, value):
        if value < 0:
            raise ValueError
        self.properties['control_delay'] = value

    @property
    def integration_time(self):
        """float: integration time of the AO system integral controller [s]."""
        return self.properties['integration_time']

    @integration_time.setter
    def integration_time(self, value):
        if value < 0:
            raise ValueError
        self.properties['integration_time'] = value
    
    @property
    def Cn0(self):
        """float: index of refraction structure constant [m**(-2/3)]."""
        return self.properties['Cn2']

    @Cn0.setter
    def Cn2(self, value):
        if value < 0:
            raise ValueError
        self.properties['Cn2'] = value

    @property
    def wavelength(self):
        """float: wavelength of the radiation [m]."""
        return self.properties['wavelength']

    @wavelength.setter
    def wavelength(self, value):
        if value < 0:
            raise ValueError
        self.properties['wavelength'] = value

    @property
    def ground_station_alt(self):
        """float: Altitude of the ground station [km]."""
        return self.properties['ground_station_alt']

    @ground_station_alt.setter
    def ground_station_alt(self, value):
        if value < 0:
            raise ValueError
        self.properties['ground_station_alt'] = value

    @property
    def aerial_platform_alt(self):
        """float: Altitude of the aerial platform [km]."""
        return self.properties['aerial_platform_alt']

    @aerial_platform_alt.setter
    def aerial_platform_alt(self, value):
        if value < 0:
            raise ValueError
        self.properties['aerial_platform_alt'] = value

    @property
    def zenith_angle(self):
        """float: Zenith angle of aerial platform [degrees]."""
        return self.properties['zenith_angle']

    @zenith_angle.setter
    def zenith_angle(self, value):
        if value < 0:
            raise ValueError
        self.properties['zenith_angle'] = value
    
    def _compute_Cn2(self, h):
        """Compute index of refraction structure constant [Andrews/Phillips, 2005].
        Uses the Hufnagel-Valley (HV) model.

        ## Parameters
        `h` : np.ndarray
            Values of h corresponding to slant path of the channel to integrate over [m].
        ## Returns
        `Cn2` : float
            Index of refraction structure constant [m^(-2/3)].
        """
        Cn2 = cn2.hufnagel_valley(h, self.wind_speed, self.Cn0)
        return Cn2
    
    def _compute_rytov_variance_plane(self):
        """Compute rytov variance of a plane wave for a downlink channel [Andrews/Phillips, 2005].

        ## Returns
        `rytov_var` : float
            Rytov variance for given length.
        """
        ground_station_alt = self.ground_station_alt*1e3
        aerial_platform_alt = self.aerial_platform_alt*1e3
        k = 2*np.pi/self.wavelength
        integrand = lambda h : self._compute_Cn2(h)*(h - ground_station_alt)**(5/6)
        rytov_var = 2.25*k**(7/6)*sec(self.zenith_angle)**(11/6)*quad(integrand, ground_station_alt, aerial_platform_alt)[0]
        return rytov_var

    def _compute_rytov_variance_spherical(self):
        """Compute rytov variance of a spherical wave for a downlink channel [Andrews/Phillips, 2005].

        ## Returns
        `rytov_var` :float
            Rytov variance for given length.
        """
        ground_station_alt = self.ground_station_alt*1e3
        aerial_platform_alt = self.aerial_platform_alt*1e3
        k = 2*np.pi/self.wavelength
        integrand = lambda h : self._compute_Cn2(h)*(h - ground_station_alt)**(5/6)*((aerial_platform_alt - h)/(aerial_platform_alt - ground_station_alt))**(5/6)
        rytov_var = 2.25*k**(7/6)*sec(self.zenith_angle)**(11/6)*quad(integrand, ground_station_alt, aerial_platform_alt)[0]
        return rytov_var
    
    def _compute_wandering_variance(self):
        """Compute beam wandering variance for a downlink channel [Andrews/Phillips, 2005].

        ## Returns
        `wandering_var` : float
            Beam wandering variance for given length [m^2].
        """
        ground_station_alt = self.ground_station_alt*1e3
        aerial_platform_alt = self.aerial_platform_alt*1e3
        k = 2*np.pi/self.wavelength
        length = 1e3*compute_channel_length(self.ground_station_alt, self.aerial_platform_alt, self.zenith_angle)
        Lambda_0 = 2*length/(k*self.W0**2)
        Theta_0 = 1
        rytov_var = self._compute_rytov_variance_spherical()
        f = lambda h: (Theta_0 + (1 - Theta_0)*(h - ground_station_alt)/(aerial_platform_alt - ground_station_alt))**2 + 1.63*(rytov_var)**(6/5)*Lambda_0*((aerial_platform_alt - h)/(aerial_platform_alt - ground_station_alt))**(16/5)
        integrand = lambda h: self._compute_Cn2(h)*(h - ground_station_alt)**2/f(h)**(1/6)
        wandering_var = 7.25*sec(self.zenith_angle)**3*self.W0**(-1/3)*quad(integrand, ground_station_alt, aerial_platform_alt)[0]
        return wandering_var
    
    def _compute_scintillation_index_plane(self, rytov_var, length):
        """Compute aperture-averaged scintillation index of plane wave for a downlink channel [Andrews/Phillips, 2005].

        ## Parameters
        `length` : float 
            Length of the channel [m].
        `rytov_var` : float 
            Rytov variance.
        ## Returns
        `scint_index` : float
            Scintillation index for requested input parameters.
        """
        k = 2*np.pi/self.wavelength
        d = np.sqrt(k*self.rx_aperture**2/(4*length))
        first_term = 0.49*rytov_var/(1 + 0.65*d**2 + 1.11*rytov_var**(6/5))**(7/6)
        second_term = 0.51*rytov_var*(1 + 0.69*rytov_var**(6/5))**(-5/6)/(1 + 0.9*d**2 + 0.62*d**2*rytov_var**(6/5))
        return np.exp(first_term + second_term) - 1
    
    def _compute_scintillation_index_spherical(self, rytov_var, length):
        """Compute aperture-averaged scintillation index of spherical wave for a downlink channel [Andrews/Phillips, 2005].

        ## Parameters
        `length` : float 
            Length of the channel [m].
        `rytov_var` : float 
            Rytov variance.
        ## Returns
        `scint_index` : float
            Scintillation index for requested input parameters.
        """
        k = 2*np.pi/self.wavelength
        d = np.sqrt(k*self.rx_aperture**2/(4*length))
        beta_0_sq = 0.4065*rytov_var
        first_term = 0.49*beta_0_sq/(1 + 0.18*d**2 + 0.56*beta_0_sq**(6/5))**(7/6)
        second_term = 0.51*beta_0_sq*(1 + 0.69*beta_0_sq**(6/5))**(-5/6)/(1 + 0.9*d**2 + 0.62*d**2*beta_0_sq**(6/5))
        return np.exp(first_term + second_term) - 1
    
    def _compute_coherence_width_plane(self):
        """Compute coherence width of plane wave for a downlink channel [Andrews/Phillips, 2005].

        ## Returns
        `coherence_width` : float
            Coherence width for requested input parameters.
        """ 
        ground_station_alt = self.ground_station_alt*1e3
        aerial_platform_alt = self.aerial_platform_alt*1e3
        k = 2*np.pi/self.wavelength
        integrand = lambda h : self._compute_Cn2(h)
        coherence_width = (0.42*k**2*sec(self.zenith_angle)*quad(integrand, ground_station_alt, aerial_platform_alt)[0])**(-3/5)
        return coherence_width
    
    def _compute_coherence_width_spherical(self):
        """Compute coherence width of spherical wave for a downlink channel [Andrews/Phillips, 2005].

        ## Returns
        `coherence_width` : float
            Coherence width for requested input parameters.
        """ 
        ground_station_alt = self.ground_station_alt*1e3
        aerial_platform_alt = self.aerial_platform_alt*1e3
        k = 2*np.pi/self.wavelength
        integrand = lambda h : self._compute_Cn2(h)*((aerial_platform_alt - h)/(aerial_platform_alt - ground_station_alt))**(5/3)
        coherence_width = (0.42*k**2*sec(self.zenith_angle)*quad(integrand, ground_station_alt, aerial_platform_alt)[0])**(-3/5)
        return coherence_width
    
    def _compute_coherence_width_gaussian(self, length):
        """Compute coherence width of gaussian wave for an downlink channel [Andrews/Phillips, 2005].

        ## Parameters 
        `length` : float
            Length of the channel [km].
        ## Returns
        `coherence_width` : float
            Coherence width for requested input parameters.
        """ 
        ground_station_alt = self.ground_station_alt*1e3
        aerial_platform_alt = self.aerial_platform_alt*1e3
        k = 2*np.pi/self.wavelength
        z = length*1e3
        Lambda_0 = 2*z/(k*self.W0**2)
        Lambda = Lambda_0/(1 + Lambda_0**2)
        Theta = 1/(1 + Lambda_0**2)
        Theta_bar = 1 - Theta
        integrand_1 = lambda h : self._compute_Cn2(h)*(Theta + Theta_bar*(aerial_platform_alt - h)/(aerial_platform_alt - ground_station_alt))**(5/3)
        mu_1d = quad(integrand_1, ground_station_alt, aerial_platform_alt)[0]
        integrand_2 = lambda h : self._compute_Cn2(h)*((h - ground_station_alt)/(aerial_platform_alt - ground_station_alt))**(5/3)
        mu_2d = quad(integrand_2, ground_station_alt, aerial_platform_alt)[0]
        coherence_width = (np.cos(np.deg2rad(self.zenith_angle))/(0.423*k**2*(mu_1d + 0.622*mu_2d*Lambda**(11/6))))**(3/5)
        return coherence_width
    
    def _compute_long_term_beam_size_at_receiver(self, rytov_var, length):
        """Compute long-term beamsize at receiver for a downlink channel [Andrews/Phillips, 2005].

        ## Parameters
        `length` : float 
            Length of the channel [m].
        `rytov_var` : float 
            Rytov variance.
        ## Returns
        `W_LT` : float
            Long-term beamsize at receiver for requested input parameters [m].
        """
        k = 2*np.pi/self.wavelength
        return self.W0*np.sqrt(1 + (self.wavelength*length/(np.pi*self.W0**2))**2 + 1.63*rytov_var**(6/5)*2*length/(k*self.W0**2))
    
    def _compute_short_term_beam_size_at_receiver(self, long_term_beamsize, wandering_var):
        """Compute short-term beamsize at receiver for a downlink channel [Andrews/Phillips, 2005].

        ## Parameters
        `long_term_beamsize` : float 
            Long-term beamsize at the receiver [m].
        `wandering_var` : float 
            Beam wandering variance at receiver [m^2].
        ## Returns
        `W_ST` : float
            Short-term beamsize at receiver for requested input parameters [m].
        """
        return np.sqrt(long_term_beamsize**2 - wandering_var)
    
    def _compute_lognormal_parameters(self, r, R, l, short_term_beamsize, scint_index):
        """Compute mean and standard deviation of lognormal distribution [Vasylyev et al., 2018].

        ## Parameters
        `r` : float 
            Deflection radius from center of receiver aperture [m].
        `R` : float 
            Weibull distribution R parameter.
        `l` : float 
            Weibull distribution l parameter.
        `short_term_beamsize` : float 
            Short-term beamsize at receiver [m].
        `scint_index` : float
            Scintillation index of horizontal channel.
        ## Returns
        `mu, sigma` : tuple (float, float)
            Mean value (mu) and standard deviation (sigma) of lognormal distribution.
        """
        rx_radius = self.rx_aperture/2
        eta_0 = 1 - np.exp(-2*rx_radius**2/short_term_beamsize**2)
        eta_mean = eta_0*np.exp(-(r/R)**l)
        eta_var = (1 + scint_index)*eta_mean**2
        mu = -np.log(eta_mean**2/np.sqrt(eta_var))
        sigma = np.sqrt(np.log(eta_var/eta_mean**2))
        return mu, sigma

    def _compute_pdt_parameters(self, length):
        """Compute parameters useful for the calculation of the probability distribution 
        of atmospheric transmittance [Vasylyev et al., 2018].

        ## Parameters
        `length` : float
            Length of the channel [km].
        ## Returns
        `lognormal_params` : function
            Output of _compute_lognormal_parameters. When evaluated at specific deflection radius r, returns
            mean value (mu) and standard deviation (sigma) of lognormal distribution at r.
        `wandering_var` : float 
            Beam wandering variance at receiver [m^2]. 
        `W_LT` : float
            Long-term beamsize at receiver for requested input parameters [m].
        """
        z = length*1e3
        rx_radius = self.rx_aperture/2
        pointing_var = (self.pointing_error*z)**2
        rytov_var = self._compute_rytov_variance_spherical()
        scint_index = self._compute_scintillation_index_spherical(rytov_var, z)
        W_LT = self._compute_long_term_beam_size_at_receiver(rytov_var, z)
        wandering_var = (self._compute_wandering_variance() + pointing_var)*(1 - self.tracking_efficiency)
        wandering_percent = 100*np.sqrt(wandering_var)/rx_radius

        if wandering_percent > 100:
            print('Warning ! The total wandering is larger than the aperture of the receiver. Use smaller values of pointing error.')

        W_ST = self._compute_short_term_beam_size_at_receiver(W_LT, wandering_var)

        X = (rx_radius/W_ST)**2
        T0 = np.sqrt(1 - np.exp(-2*X))
        l = 8 * X * np.exp(-4*X) * i1(4*X) / (1 - np.exp(-4*X)*i0(4*X))/np.log(2*T0**2/(1 - np.exp(-4*X)*i0(4*X)))
        R = rx_radius * np.log(2*T0**2/(1 - np.exp(-4*X)*i0(4*X)))**(-1./l)

        lognormal_params = lambda r : self._compute_lognormal_parameters(r, R, l, W_ST, scint_index)

        return lognormal_params, wandering_var, W_LT
    
    def _compute_pdt(self, eta, length):
        """Compute probability distribution of atmospheric transmittance (PDT) [Vasylyev et al., 2018].

        ## Parameters
        `eta` : np.ndarray
            Input random variable values to calculate PDT for.
        `length` : float
            Length of the channel [km].
        ## Returns
        `integral` : np.ndarray
            PDT function for input eta.
        """
        lognormal_params, wandering_var, W_LT = self._compute_pdt_parameters(length)
        if wandering_var == 0: 
            pdt = truncated_lognormal_pdf(eta, lognormal_params(0)[0], lognormal_params(0)[1])
        else:   
            integrand = lambda r: r*truncated_lognormal_pdf(eta, lognormal_params(r)[0], lognormal_params(r)[1])*np.exp(-r**2/(2*wandering_var))/wandering_var
            pdt = quad_vec(integrand, 0, self.rx_aperture/2 + W_LT)[0]
        return pdt

    def _compute_conversion_matrix(self, j_max):
        """Compute conversion matrix [Canuet et al., 2019].
        """
        Z = smf.compute_zernike(j_max)
        CZZ = smf.calculate_CZZ(self.rx_aperture/2, self.rx_aperture/2, Z, j_max, self.obs_ratio)
        M = smf.compute_conversion_matrix(CZZ)
        return M

    def _compute_attenuation_factors(self):
        """Compute attenuation factors of turbulent phase mode variances up to maximum order of correction n_max [Roddier, 1999].

        ## Returns
        `gamma_j` : np.ndarray
            Attenuation factors.
        """

        n = lut_zernike_index_pd["n"]
        n = np.array(lut_zernike_index_pd["n"].values)
        n_corrected = n[n <= self.n_max]
        open_loop_tf = lambda v: self.integral_gain*np.exp(-self.control_delay*v)*(1 - np.exp(-self.integration_time*v))/(self.integration_time*v)**2
        e_error = lambda v: 1/(1 + open_loop_tf(v))
        gamma_j = np.ones_like(n, dtype = float)
        cutoff_freq = 0.3*(n_corrected + 1)*self.wind_speed/self.rx_aperture
        for index in range(0, np.size(n_corrected)):
            if n_corrected[index] == 1:
                PSD_turbulence = lambda v: v**(-2/3) if v <= cutoff_freq[index] else v**(-17/3)
            else:
                PSD_turbulence = lambda v: v**(0) if v <= cutoff_freq[index] else v**(-17/3)
            gamma_j[index] = quad(lambda v: e_error(v)**2*PSD_turbulence(v), 1e-2, np.inf)[0]/quad(PSD_turbulence, 1e-2, np.inf)[0]

        return gamma_j
    
    def _compute_smf_coupling_pdf(self, eta_smf, eta_max, length):
        """Compute probability density function (PDF) of single mode fiber (SMF) coupling efficiency [Canuet et al., 2018].

        ## Parameters
        `eta_smf` : np.ndarray
            Input random variable values to calculate pdf for.
        `eta_max` : float
            Theoretical maximum coupling efficiency.
        `length` : float
            Length of the channel [km].
        ## Returns
        `smf_pdf` : np.ndarray
            SMF PDF for input eta.
        """
        z = length*1e3
        n = np.array(lut_zernike_index_pd["n"].values)
        j_Noll_as_index = np.array(lut_zernike_index_pd["j_Noll"].values) - 2
        rytov_var = self._compute_rytov_variance_spherical()

        #Check of the condition for aperture averaging
        if rytov_var <1:
                check = np.sqrt(self.wavelength*length*1e3)
        else:
                check = 0.36*np.sqrt(self.wavelength*length*1e3)* (rytov_var**(-3/5))
        if self.rx_aperture < check:
            print("Warning ! The aperture averaging hypothesis is not valid for this set of parameters. Use bigger values of receiving aperture size")
        
        scint_index = self._compute_scintillation_index_spherical(rytov_var, z)
        r0 = self._compute_coherence_width_gaussian(z)
        eta_s = np.exp(-np.log(1 + scint_index))
        bj2 = smf.bn2(self.rx_aperture, r0, n, self.obs_ratio)
        gamma_j = self._compute_attenuation_factors()
        bj2 = bj2*gamma_j

        # Check if we are below the Rayleigh criterion
        bj_wvln = np.sqrt(bj2)/(2*np.pi)
        bj_wlvn_max = np.max(bj_wvln)
        if bj_wlvn_max > 0.05:
            print(f" Warning ! The maximum Zernike coefficient std in wavelenghts is {bj_wlvn_max}. The SMF PDF is accurate below the Rayleigh criterion (0.05). You may need to use higher order of correction or smaller integration time of the AO system.")

        beta_opt = smf.beta_opt(self.obs_ratio)
        eta_smf_max = smf.eta_0(self.obs_ratio, beta_opt)
        eta_max = eta_max*eta_s*eta_smf_max
        smf_pdf = smf.compute_eta_smf_probability_distribution(eta_smf, eta_max, bj2)
        return smf_pdf
    
    def _compute_channel_pdf(self, eta_ch, length):
        """Compute probability density function (PDF) of free-space channel efficiency [Scriminich et al., 2022].

        ## Parameters
        `eta_ch` : np.ndarray
            Input random variable values to calculate pdf for.
        `length` : float
            Length of the channel [km].
        ## Returns
        `ch_pdf` : np.ndarray
            Channel PDF for input eta.
        """
        N = 10
        pdt = self._compute_pdt(eta_ch, length)
        pdt = pdt/np.sum(pdt)
        eta_rx = np.random.choice(eta_ch, N, p = pdt)
        integral = 0
        for index in range(0, N):
            integral = integral + self._compute_smf_coupling_pdf(eta_ch, eta_rx[index], length)
        ch_pdf = integral
        ch_pdf = self.Tatm*ch_pdf/np.sum(ch_pdf)
        return ch_pdf
    
    def _compute_mean_channel_efficiency(self, eta_ch, length, detector_efficiency = 1):
        """Compute mean channel efficiency, including losses at the detector.

        ## Parameters
        `eta_ch` : np.ndarray
            Input random variable values to calculate pdf for.
        `length` : float 
            Length of the channel [km].
        `detector_efficiency` : float
            Efficiency of detector at receiver (default 1).
        ## Returns
        `ch_pdf` : np.ndarray
            Channel PDF for input eta.
        """
        pdt = self._compute_pdt(eta_ch, length)
        pdt = pdt/np.sum(pdt)
        
        z = length*1e3
        n = np.array(lut_zernike_index_pd["n"].values)
        j_Noll_as_index = np.array(lut_zernike_index_pd["j_Noll"].values) - 2

        rytov_var = self._compute_rytov_variance_spherical()

        #Check of the condition for aperture averaging
        if rytov_var <1:
                check = np.sqrt(self.wavelength*length*1e3)
        else:
                check = 0.36*np.sqrt(self.wavelength*length*1e3)* (rytov_var**(-3/5))
        if self.rx_aperture < check:
            print("Warning ! The aperture averaging hypothesis is not valid for this set of parameters. Use bigger values of receiving aperture size")

        scint_index = self._compute_scintillation_index_spherical(rytov_var, z)
        r0 = self._compute_coherence_width_gaussian(z)
        eta_s = np.exp(-np.log(1 + scint_index))
        bj2 = smf.bn2(self.rx_aperture, r0, n,self.obs_ratio)

        gamma_j = self._compute_attenuation_factors()
        bj2 = bj2*gamma_j

        # Check if we are below the Rayleigh criterion
        bj_wvln = np.sqrt(bj2)/(2*np.pi)
        bj_wlvn_max = np.max(bj_wvln)
        if bj_wlvn_max > 0.05:
            print(f" Warning ! The maximum Zernike coefficient std in wavelenghts is {bj_wlvn_max}. The SMF PDF is accurate below the Rayleigh criterion (0.05). You may need to use higher order of correction or smaller integration time of the AO system.")

        beta_opt = smf.beta_opt(self.obs_ratio)
        eta_smf_max = smf.eta_0(self.obs_ratio, beta_opt)

        mean_transmittance = np.sum(eta_ch*pdt)*self.Tatm*eta_s*eta_smf_max*detector_efficiency *smf.eta_ao(bj2)
        return mean_transmittance
    
    def _draw_pdt_sample(self, length):
        """Draw random sample from probability distribution of atmospheric transmittance (PDT).

        ## Parameters
        `length` : float
            Length of the channel [km].
        ## Returns
        `sample` : float
            Random sample of PDT.
        """
        eta = np.linspace(1e-7, 1, 1000)
        pdt = self._compute_pdt(eta, length)
        pdt = np.abs(pdt/np.sum(pdt))
        sample = np.random.choice(eta, 1, p = pdt)
        return sample
    
    def _draw_smf_pdf_sample(self, length):
        """Draw random sample from probability distribution of single-mode fiber (SMF) coupling efficiency [Canuet et al., 2018].

        ## Parameters
        `length` : float
            Length of the channel [km].
        ## Returns
        `sample` : float
            Random sample of PDT.
        """
        eta = np.linspace(1e-7, 1, 1000)
        smf_pdf = self._compute_smf_coupling_pdf(eta, 1, length)
        smf_pdf = np.abs(smf_pdf/np.sum(smf_pdf))
        plt.figure()
        plt.plot(eta, smf_pdf)
        plt.show()
        sample = np.random.choice(eta, 1, p = smf_pdf)
        return sample
    
    def _draw_channel_pdf_sample(self, length, n_samples):
        """Draw random sample from free-space channel probability distribution [Scriminich et al., 2022].
        To be more efficient, the sample is calculated as the product of a sample from the PDT and the SMF coupling efficiency PDF,
        instead of the function of the channel PDF. 

        ## Parameters
        `length` : float
            Length of the channel [km].
        `n_samples` : int
            Number of samples to return.
        ## Returns
        `sample` : float
            Random sample of channel PDF.
        """
        eta = np.linspace(1e-4, 1, 1000)
        pdt = self._compute_pdt(eta, length)
        pdt = np.abs(pdt/np.sum(pdt))
        pdt /= pdt.sum()
        smf_pdf = self._compute_smf_coupling_pdf(eta, 1, length)
        smf_pdf = np.abs(smf_pdf/np.sum(smf_pdf))
        smf_pdf /= smf_pdf.sum()
        pdt_sample = np.random.choice(eta, n_samples, p = pdt)
        smf_sample = np.random.choice(eta, n_samples, p = smf_pdf)
        sample = self.Tatm*pdt_sample*smf_sample
        return sample
    
    def _compute_loss_probability(self, length, n_samples):
        """Compute loss probability of photon in downlink channel, taking all losses into account.

        ## Parameters
        `length` : float
            Length of the channel [km].
        `n_samples` : int
            Number of samples to return.
        ## Returns
        `prob_loss` : float
            Probability that a photon is lost in the channel.
        """
        T = self._draw_channel_pdf_sample(length, n_samples)
        prob_loss = 1 - T
        return prob_loss
    
    def error_operation(self, qubits, **kwargs):
        """Error operation to apply to qubits.

        Parameters
        ----------
        qubits : tuple of :obj:`~netsquid.qubits.qubit.Qubit`
            Qubits to apply noise to.

        """
        if 'channel' in kwargs:
            warn_deprecated("channel parameter is deprecated. "
                            "Pass length parameter directly instead.",
                            key="FreeSpaceLossModel.compute_model.channel")
            kwargs['length'] = kwargs['channel'].properties["length"]
            del kwargs['channel']

        prob_loss = self._compute_loss_probability(length = kwargs['length'], n_samples = len(qubits))
        for idx, qubit in enumerate(qubits):
            if qubit is None:
                continue
            self.lose_qubit(qubits, idx, prob_loss[idx], rng = self.properties['rng'])
            
class CachedChannel(QuantumErrorModel):
    """Class that performs error operation on qubits from precalculated probability of loss samples saved in an array
    to speed up execution time.

    ## Parameters
    ----------
    `loss_array` : np.ndarray
        Probability of loss samples array.
    `rng` : :obj:`~numpy.random.RandomState` or None, optional
        Random number generator to use. If ``None`` then
        :obj:`~netsquid.util.simtools.get_random_state` is used.
    """
    
    def __init__(self, loss_array, rng = None):
        super().__init__()
        self.loss_array = loss_array
        self.rng = rng if rng else simtools.get_random_state()
        
    @property
    def rng(self):
        """ :obj:`~numpy.random.RandomState`: Random number generator."""
        return self.properties['rng']
    
    @rng.setter
    def rng(self, value):
        if not isinstance(value, np.random.RandomState):
            raise TypeError("{} is not a valid numpy RandomState".format(value))
        self.properties['rng'] = value      

    @property
    def loss_array(self):
        """ :np.ndarray: probability of loss samples array. """
        return self.properties['loss_array']

    def loss_array(self, value):
        if np.any((value < 0) | (value > 1)):  # Properly use '|' for element-wise logical OR
            raise ValueError("Values in loss_array must be in the range [0, 1]")
        self.properties['loss_array'] = value
    
    def error_operation(self, qubits, **kwargs):
        """Error operation to apply to qubits.

        Parameters
        ----------
        qubits : tuple of :obj:`~netsquid.qubits.qubit.Qubit`
        Qubits to apply noise to.

        """
        if 'channel' in kwargs:
            warn_deprecated("channel parameter is deprecated. "
                            "Pass length parameter directly instead.",
                            key="FreeSpaceLossModel.compute_model.channel")
            kwargs['length'] = kwargs['channel'].properties["length"]
            del kwargs['channel']

        for idx, qubit in enumerate(qubits):
            if qubit is None:
                continue
            prob_loss = random.choice(self.loss_array)
            print(prob_loss)
            self.lose_qubit(qubits, idx, prob_loss, rng=self.properties['rng'])

class UplinkChannel(DownlinkChannel):
    """Model for photon loss on an uplink free-space channel.

    In the current implementation and considering the principle of reciprocity, the same model as
    the downlink is used (which is inherited), with the difference that the anisoplanatic error is 
    taken into account when calculating the residual zernike coefficient variances after correction.

    ## Parameters
    ----------
    `R_rx` : float
        Radius of the receiver aperture on the balloon [m].
    `D_tx` : float
        Diameter of the transmitting telescope [m].
    `obs_ratio` : float
        Obscuration ratio of the transmitting telescope.
    `n_max` : int
        Maximum radial index of correction of AO system.
    `Cn0` : float
        Reference index of refraction structure constant at ground level [m**(-2/3)].
    `wind_speed` : float
        Rms speed of the wind [m/s].  
    `wavelength` : float
        Wavelength of the radiation [m].
    `ground_station_alt` : float 
        Altitude of the ground station [km].
    `aerial_platform_alt` : float 
        Altitude of the aerial platform [km].
    `zenith_angle` : float
        Zenith angle of aerial platform [degrees].
    `pointing_error` : float
        Pointing error [rad].
    `tracking_efficiency` : float
        Efficiency of the coarse tracking mechanism.
    `Tatm` : float
        Atmospheric transmittance (square of the transmission coefficient).
    `integral_gain: float`
        Integral gain of the AO system integral controller.
    `control_delay: float`
        Delay of the AO system loop [s].
    `integration_time: float`
        Integration time of the AO system integral controller [s].
    `rng` : :obj:`~numpy.random.RandomState` or None, optional
        Random number generator to use. If ``None`` then
        :obj:`~netsquid.util.simtools.get_random_state` is used.
    """

    def __init__(self, R_rx, D_tx, obs_ratio, n_max, Cn0, wind_speed, wavelength, 
                 ground_station_alt, aerial_platform_alt, zenith_angle, pointing_error = 0, 
                 tracking_efficiency = 0, Tatm = 1, integral_gain = 1, control_delay = 13.32e-4, integration_time = 6.66e-4, rng = None):
        super().__init__(R_rx, D_tx, obs_ratio, n_max, Cn0, wind_speed, wavelength, 
                         ground_station_alt, aerial_platform_alt, zenith_angle, pointing_error, 
                         tracking_efficiency, Tatm, integral_gain, control_delay, integration_time, rng)
        self.D_tx = D_tx
        self.R_rx = R_rx
        self._compute_mean_channel_efficiency(eta_ch=0.18, length=aerial_platform_alt)
        
    def _compute_anisoplanatic_error(self, length):
        """Compute anisoplanatic error

        ## Parameters
        `length` : float
            Length of the channel [m]
        ## Returns
        `var_aniso` : float
            Wavefront error variance attributed to anisoplanatism.
        """
        ground_station_alt = self.ground_station_alt*1e3
        aerial_platform_alt = self.aerial_platform_alt*1e3
        k = 2*np.pi/self.wavelength

        Lambda_0 = 2*length/(k*self.R_rx**2)
        Lambda = Lambda_0/(1 + Lambda_0**2)
        Theta = 1/(1 + Lambda_0**2)
        Theta_bar = 1 - Theta
        integrand_1 = lambda h : self._compute_Cn2(h)*(Theta + Theta_bar*(h - ground_station_alt)/(aerial_platform_alt - ground_station_alt))**(5/3)
        mu_1u = quad(integrand_1, ground_station_alt, aerial_platform_alt)[0]
        integrand_2 = lambda h : self._compute_Cn2(h)*((aerial_platform_alt - h)/(aerial_platform_alt - ground_station_alt))**(5/3)
        mu_2u = quad(integrand_2, ground_station_alt, aerial_platform_alt)[0]
        isoplanatic = (np.cos(np.deg2rad(self.zenith_angle))**(8/5))/(self.aerial_platform_alt - self.ground_station_alt)/(2.91*k**2*(mu_1u + 0.62*mu_2u*Lambda**(11/6)))**(3/5)

        var_isoplanatic = (self.pointing_error/isoplanatic)**(5/3)

        return var_isoplanatic

    def _compute_smf_coupling_pdf(self, eta_smf, eta_max, length):
        """Compute probability density function (PDF) of single mode fiber (SMF) coupling efficiency [Canuet et al., 2018].
        Overwrites the parent DownlinkChannel method to include isoplanatic error.

        ## Parameters
        `eta_smf` : np.ndarray
            Input random variable values to calculate pdf for.
        `eta_max` : float
            Theoretical maximum coupling efficiency.
        `length` : float
            Length of the channel [km].
        ## Returns
        `smf_pdf` : np.ndarray
            SMF PDF for input eta.
        """
        z = length*1e3
        n = np.array(lut_zernike_index_pd["n"].values)
        rytov_var = self._compute_rytov_variance_spherical()

        #Check of the condition for aperture averaging
        if rytov_var <1:
                check = np.sqrt(self.wavelength*length*1e3)
        else:
                check = 0.36*np.sqrt(self.wavelength*length*1e3)* (rytov_var**(-3/5))
        if self.rx_aperture < check:
            print("Problem aperture averaging: AO level : {} , Aperture : {} , check: {}".format(self.n_max,self.rx_aperture,check))
            # raise ValueError('The aperture averaging hypothesis is not valid for this set of parameters. Use bigger values of receiving aperture size')
        scint_index = self._compute_scintillation_index_spherical(rytov_var, z)
        r0 = self._compute_coherence_width_gaussian(z)
        eta_s = np.exp(-np.log(1 + scint_index))
        bj2 = smf.bn2(self.D_tx, r0, n,self.obs_ratio)

        gamma_j = self._compute_attenuation_factors()
        bj2 = bj2*gamma_j

        # Check if we are below the Rayleigh criterion
        bj_wvln = np.sqrt(bj2)/(2*np.pi)
        bj_wlvn_max = np.max(bj_wvln)
        if bj_wlvn_max > 0.05:
            print("Problem Rayleigh: AO level : {} , check: {}".format(self.n_max,bj_wlvn_max))
            # raise ValueError(f"The maximum Zernike coefficient std in wavelenghts is {bj_wlvn_max}. The SMF PDF is accurate below the Rayleigh criterion (0.05). You may need to use higher order of correction or smaller integration time of the AO system.")
        
        # Compute anisoplanatic error
        var_aniso = self._compute_anisoplanatic_error(z)

        beta_opt = smf.beta_opt(self.obs_ratio)
        eta_smf_max = smf.eta_0(self.obs_ratio, beta_opt)
        eta_max = eta_max*eta_s*eta_smf_max*np.exp(-var_aniso)
        smf_pdf = smf.compute_eta_smf_probability_distribution(eta_smf, eta_max, bj2)
        return smf_pdf

    def _compute_mean_channel_efficiency(self, eta_ch, length, detector_efficiency = 1):
        """Compute mean channel efficiency, including losses at the detector.
        Overwrites the parent DownlinkChannel method to include isoplanatic error.

        ## Parameters
        `eta_ch` : np.ndarray
            Input random variable values to calculate pdf for.
        `length` : float 
            Length of the channel [km].
        `detector_efficiency` : float
            Efficiency of detector at receiver (default 1).
        ## Returns
        `ch_pdf` : np.ndarray
            Channel PDF for input eta.
        """
        pdt = self._compute_pdt(eta_ch, length)
        pdt = pdt/np.sum(pdt)
        
        z = length*1e3
        n = np.array(lut_zernike_index_pd["n"].values)
        rytov_var = self._compute_rytov_variance_spherical()

        #Check of the condition for aperture averaging
        if rytov_var <1:
                check = np.sqrt(self.wavelength*length*1e3)
        else:
                check = 0.36*np.sqrt(self.wavelength*length*1e3)* (rytov_var**(-3/5))
        if self.rx_aperture < check:
            print("Problem aperture averaging: AO level : {} , Aperture : {} , check: {}".format(self.n_max,self.rx_aperture,check))
            # raise ValueError('The aperture averaging hypothesis is not valid for this set of parameters. Use bigger values of receiving aperture size')
        
        scint_index = self._compute_scintillation_index_spherical(rytov_var, z)
        r0 = self._compute_coherence_width_gaussian(z)
        eta_s = np.exp(-np.log(1 + scint_index))
        bj2 = smf.bn2(self.rx_aperture, r0, n,self.obs_ratio)

        gamma_j = self._compute_attenuation_factors()
        bj2 = bj2*gamma_j

        # Check if we are below the Rayleigh criterion
        bj_wvln = np.sqrt(bj2)/(2*np.pi)
        bj_wlvn_max = np.max(bj_wvln)
        if bj_wlvn_max > 0.05:
            print("Problem Rayleigh: AO level : {} , check: {}".format(self.n_max,bj_wlvn_max))
            # raise ValueError(f"The maximum Zernike coefficient std in wavelenghts is {bj_wlvn_max}. The SMF PDF is accurate below the Rayleigh criterion (0.05). You may need to use higher order of correction or smaller integration time of the AO system.")

        # Compute anisoplanatic error
        var_aniso = self._compute_anisoplanatic_error(z)

        beta_opt = smf.beta_opt(self.obs_ratio)
        eta_smf_max = smf.eta_0(self.obs_ratio, beta_opt)

        mean_transmittance = np.sum(eta_ch*pdt)*self.Tatm*smf.eta_ao(bj2)*eta_s*eta_smf_max*np.exp(-var_aniso)*detector_efficiency
        return mean_transmittance

class FibreDepolarizeModel(QuantumErrorModel):
    """Custom non-physical error model used to show the effectiveness
    of repeater chains.
    Taken from Netsquid's official examples.

    The default values are chosen to make a nice figure,
    and don't represent any physical system.

    Parameters
    ----------
    p_depol_init : float, optional
        Probability of depolarization on entering a fibre.
        Must be between 0 and 1. Default 0.009
    p_depol_length : float, optional
        Probability of depolarization per km of fibre.
        Must be between 0 and 1. Default 0.025

    """
    def __init__(self, p_depol_init=0.09, p_depol_length=0.025):
        super().__init__()
        self.properties['p_depol_init'] = p_depol_init
        self.properties['p_depol_length'] = p_depol_length
        self.required_properties = ['length']

    def error_operation(self, qubits, delta_time=0, **kwargs):
        """Uses the length property to calculate a depolarization probability,
        and applies it to the qubits.

        Parameters
        ----------
        qubits : tuple of :obj:`~netsquid.qubits.qubit.Qubit`
            Qubits to apply noise to.
        delta_time : float, optional
            Time qubits have spent on a component [ns]. Not used.

        """
        for qubit in qubits:
            prob = 1 - (1 - self.properties['p_depol_init']) * np.power(
                10, - kwargs['length']**2 * self.properties['p_depol_length'] / 10)
            ns.qubits.depolarize(qubit, prob=prob)
            
class FibreDepolGaussModel(QuantumErrorModel):
    """
    Custom depolarization model, empirically obtained from https://arxiv.org/abs/0801.3620.
    It uses polarization mode dispersion time to evaluate the probability of depolarization.


    """
    def __init__(self):
        super().__init__()
        self.required_properties = ['length']

    def error_operation(self, qubits, delta_time=0, **kwargs):
        """Uses the length property to calculate a depolarization probability,
        and applies it to the qubits.

        Parameters
        ----------
        qubits : tuple of :obj:`~netsquid.qubits.qubit.Qubit`
            Qubits to apply noise to.
        delta_time : float, optional
            Time qubits have spent on a component [ns]. Not used.

        """
        for qubit in qubits:
            dgd=0.6*np.sqrt(float(kwargs['length'])/50)
            tau=gauss(dgd,dgd)
            tdec=1.6
            if tau >= tdec:
                prob=1
            elif tau < tdec:
                prob=0
            ns.qubits.depolarize(qubit, prob=prob)


class FreeSpaceLossModel(QuantumErrorModel):
    """Model for photon loss on a free space channel

    Uses beam-wandering PDTC from [Vasylyev et al., PRL 108, 220501 (2012)] to
    sample the loss probability of the photon.

    Parameters
    ----------
    W0 : float
        Waist of the beam at the transmitter [m].
    rx_aperture : float
        Radius of the receiving telescope [m].
    Cn2 : float
        Index of refraction structure constant [m**(-2/3)].
    wavelength : float
        Wavelength of the radiation [m].
    Tatm : float
        Atmospheric transmittance (square of the transmission coefficient).
    rng : :obj:`~numpy.random.RandomState` or None, optional
        Random number generator to use. If ``None`` then
        :obj:`~netsquid.util.simtools.get_random_state` is used.
    """
    def __init__(self, wavelength=1550*1e-9, Tatm=1, rng=None):
        super().__init__()
        self.rng = rng if rng else simtools.get_random_state()
        self.W0 = wavelength/(5e-6*np.pi)
        self.rx_aperture = 0.4 
        self.Cn2 = 10e-18 
        self.wavelength = wavelength
        self.Tatm = Tatm
        self.required_properties = ['length']

    @property
    def rng(self):
        """ :obj:`~numpy.random.RandomState`: Random number generator."""
        return self.properties['rng']

    @rng.setter
    def rng(self, value):
        if not isinstance(value, np.random.RandomState):
            raise TypeError("{} is not a valid numpy RandomState".format(value))
        self.properties['rng'] = value
        
    @property
    def Tatm(self):
        """ :float: atmosphere transmittance. """
        return self.properties['Tatm']

    @Tatm.setter
    def Tatm(self, value):
        if (value < 0) or (value > 1):
            raise ValueError
        self.properties['Tatm'] = value

    @property
    def W0(self):
        """float: beam waist at the transmitter [m]."""
        return self.properties['W0']

    @W0.setter
    def W0(self, value):
        if value < 0:
            raise ValueError
        self.properties['W0'] = value

    @property
    def rx_aperture(self):
        """float: radius of the receiving telescope [m]."""
        return self.properties['rx_aperture']

    @rx_aperture.setter
    def rx_aperture(self, value):
        if value < 0:
            raise ValueError
        self.properties['rx_aperture'] = value

    @property
    def Cn2(self):
        """float: index of refraction structure constant [m**(-2/3)]."""
        return self.properties['Cn2']

    @Cn2.setter
    def Cn2(self, value):
        if value < 0:
            raise ValueError
        self.properties['Cn2'] = value

    @property
    def wavelength(self):
        """float: wavelength of the radiation [m]."""
        return self.properties['wavelength']

    @wavelength.setter
    def wavelength(self, value):
        if value < 0:
            raise ValueError
        self.properties['wavelength'] = value

    def _compute_weibull_loss_model_parameters(self, length):
        """
        Parameters
        ----------
        length: float
            Length of the channel.

        Returns
        -------
        tuple (float, float, float)
            The elements of the tuple are properties of the
            Weibull distribution. From left to right:
            - the 'shape' parameter
            - the 'scale' parameter
            - 'T0'
        """
        # TODO improve explanation in the docstring of this method
        # TODO explain the model below in the main docstring of this class
        # calculate the parameters used in the PDTC
        
        z = length*1e3
        W = self.W0*np.sqrt(1 + (z*self.wavelength/(np.pi*self.W0**2))**2)
        X = (self.rx_aperture/W)**2
        T0 = np.sqrt(1 - np.exp(-2*X))
        sigma = np.sqrt(1.919 * self.Cn2 * z**3 * (2*self.W0)**(-1./3.))
        l = 8 * X * np.exp(-4*X) * i1(4*X) / (1 - np.exp(-4*X)*i0(4*X)) / np.log( 2*T0**2/(1-np.exp(-4*X)*i0(4*X)))
        R = self.rx_aperture * np.log( 2*T0**2/(1-np.exp(-4*X)*i0(4*X)) )**(-1./l)

        # define the parameters of the Weibull distribution
        a = 2/l
        scaleL = (2*(sigma/R)**2)**(l/2)

        return (a, scaleL, T0)

    def _sample_loss_probability(self, length):
        """
        Parameters
        ----------
        length: float
            Length of the channel.

        Returns
        -------
        float
        """
        a, scaleL, T0 = self._compute_weibull_loss_model_parameters(length=length)

        # extract the value of the transmission coefficient
        # print('Transmission coefficient extraction')
        x = weibull(a, 1)
        scaleX = scaleL * x
        T = T0*np.exp(-scaleX/2)
        # print('T =',T)
        # calculate the probability of losing the qubit
        prob_loss = 1 - self.Tatm * T**2
        return prob_loss

    def error_operation(self, qubits, delta_time=0, **kwargs):
        """Error operation to apply to qubits.

        Parameters
        ----------
        qubits : tuple of :obj:`~netsquid.qubits.qubit.Qubit`
            Qubits to apply noise to.
        delta_time : float, optional
            Time qubits have spent on a component [ns].

        """
        if 'channel' in kwargs:
            warn_deprecated("channel parameter is deprecated. "
                            "Pass length parameter directly instead.",
                            key="FreeSpaceLossModel.compute_model.channel")
            kwargs['length'] = kwargs['channel'].properties["length"]
            del kwargs['channel']

#        # test the distribution
#        x = weibull(a,10000)
#        scaleX = scaleL * x
#        T = T0*np.exp(-scaleX/2)
#        import matplotlib.pyplot as plt
#        plt.hist(T,1000)

        for idx, qubit in enumerate(qubits):
            if qubit is None:
                continue
            prob_loss = self._sample_loss_probability(length=kwargs['length'])
#            print(prob_loss)
            self.lose_qubit(qubits, idx, prob_loss, rng=self.properties['rng'])

class FixedSatelliteLossModel(FreeSpaceLossModel):
    """Model for photon loss on a satellite-to-ground static channel

    Uses beam-wandering PDTC from [Vasylyev et al., PRL 108, 220501 (2012)] to
    sample the loss probability of the photon.

    Parameters
    ----------
    txDiv : float
        Divergence of the beam sent from the satellite [rad].
    sigmaPoint :
        Pointing error of the satellite, standard deviation [rad].
    rx_aperture : float
        Radius of the receiving telescope [m].
    Cn2 : float
        Index of refraction structure constant [m**(-2/3)].
    wavelength : float
        Wavelength of the radiation [m].
    Tatm : float
        Atmospheric transmittance (square of the transmission coefficient).
    rng : :obj:`~numpy.random.RandomState` or None, optional
        Random number generator to use. If ``None`` then
        :obj:`~netsquid.util.simtools.get_random_state` is used.
    """
    def __init__(self, txDiv=5e-6, sigmaPoint=0.5e-6, wavelength=1550*1e-9, Tatm=1, rng=None):
        super().__init__(wavelength,Tatm,rng)
        self.txDiv = txDiv
        self.sigmaPoint = sigmaPoint
        self.required_properties = ['length']

    @property
    def txDiv(self):
        """float: divergence of the beam at the transmitter (satellite) [m]."""
        return self.properties['txDiv']

    @txDiv.setter
    def txDiv(self, value):
        if value < 0:
            raise ValueError
        self.properties['txDiv'] = value

    @property
    def sigmaPoint(self):
        """float: pointing error at the transmitter (satellite) [m]."""
        return self.properties['sigmaPoint']

    @sigmaPoint.setter
    def sigmaPoint(self, value):
        if value < 0:
            raise ValueError
        self.properties['sigmaPoint'] = value
        
    def _compute_weibull_loss_model_parameters(self, length):
        """
        Parameters
        ----------
        length: float
            Length of the channel.

        Returns
        -------
        tuple (float, float, float)
            The elements of the tuple are properties of the
            Weibull distribution. From left to right:
            - the 'shape' parameter
            - the 'scale' parameter
            - 'T0'
        """
        # TODO improve explanation in the docstring of this method
        # TODO explain the model below in the main docstring of this class
        # calculate the parameters used in the PDTC
        
        # this function cannot be used for range values lower than 10 km
        if length <= 10:
            raise ValueError
        
        z = length*1e3
        W = self.txDiv * z
        X = (self.rx_aperture/W)**2
        T0 = np.sqrt(1 - np.exp(-2*X))
        sigmaTurb = np.sqrt(1.919 * self.Cn2 * 10e3**3 * (2*self.txDiv*(z-10e3))**(-1./3.))
        sigma = np.sqrt( (self.sigmaPoint*z)**2 + sigmaTurb**2 )
        l = 8 * X * np.exp(-4*X) * i1(4*X) / (1 - np.exp(-4*X)*i0(4*X)) / np.log( 2*T0**2/(1-np.exp(-4*X)*i0(4*X)))
        R = self.rx_aperture * np.log( 2*T0**2/(1-np.exp(-4*X)*i0(4*X)) )**(-1./l)

        # define the parameters of the Weibull distribution
        a = 2/l
        scaleL = (2*(sigma/R)**2)**(l/2)

        return (a, scaleL, T0)

class ClassicalConnection(Connection):
    """A connection that transmits classical messages in one direction, from A to B.
    Copied from official NetSquid's examples (teleportation)

    Parameters
    ----------
    length : float
        End to end length of the connection [km].
    name : str, optional
       Name of this connection.

    """

    def __init__(self, length, name="ClassicalConnection"):
        super().__init__(name=name)
        self.add_subcomponent(ClassicalChannel("Channel_A2B", length=length,
                                               models={"delay_model": FibreDelayModel()}),
                              forward_input=[("A", "send")],
                              forward_output=[("B", "recv")])
        
