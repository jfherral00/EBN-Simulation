from icecream import ic
import netsquid as ns
import networkx as nx
import numpy as np
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
from utils import dc_setup
from random import gauss

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
        #TODO: Change queue to quantum memory queue?
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
        if mode not in ['name','simulation_duration','epr_pair','link_fidel_rounds','path_fidel_rounds','nodes','links','requests']:
            raise ValueError('Unsupported mode')
        else:
            elements = self._config[mode] 
            #Querying for a global property
            if mode in ['name','epr_pair','simulation_duration','link_fidel_rounds','path_fidel_rounds']: 
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

                    #Create classical connection. We create channels even if purification is not needed
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
        
