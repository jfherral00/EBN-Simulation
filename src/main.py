from network import NetworkManager
from icecream import ic
import pandas as pd
import numpy as np
import logging
import os
import netsquid as ns
from netsquid.util import simlog
from utils import generate_report, validate_conf, check_parameter, load_config, create_plot
import yaml
import datetime
from applications import CapacityApplication, TeleportationApplication, CHSHApplication
import copy

try:
    from pylatex import Document
    print_report = True
except:
    print_report = False

'''
logger = logging.getLogger('netsquid')
simlog.logger.setLevel(logging.DEBUG)
# Create a file handler and set the filename
log_file_path = 'simulation.log'
file_handler = logging.FileHandler(log_file_path)

# Set the logging format
formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
file_handler.setFormatter(formatter)

# Add the file handler to the logger
logger.addHandler(file_handler)
'''

file = './network_config.yaml'

#Read configuration file
with open(file,'r') as config_file:
    config = yaml.safe_load(config_file)

#Create output directory in case it doesn't exist
try:
    os.stat('./output')
except:
    os.mkdir('./output')

#Ask for execution mode: fixed or evolution
mode = input('Do you want to perform fixed parameter simulation or evolution? (F: Fixed, E: Evolution): ')
if mode == 'F':
    steps = 1
    element = 'FixedSimul'
    prop = 'FixedSimul'
    value = 0
    vals = [0]
    min_val='-'
    max_val='-'
    #Validate configuration file
    iter_config = copy.deepcopy(config)
    validate_conf(iter_config)
elif mode == 'E':
    element = input('Enter object (nodes/links/requests). Parameter will be set in ALL instances: ')
    prop = input('Enter property: ')
    if not check_parameter(element, prop):
        raise ValueError("Evolution for that parameter not supported")
    
    min_val = float(input('Enter minimum value: '))

    max_val = float(input('Enter maximum value: '))
    if max_val <= min_val: raise ValueError('Maximum must be greater than minimum')

    steps = int(input('Enter number of steps (minimum 2): '))
    if steps <= 1: raise ValueError('Minumum of 2 steps needed')
    
    scale = input('Do you want data points in (L)og scale or equally (S)paced? (L/S)')
    if scale == 'L':
        pass
        vals = np.geomspace(min_val, max_val, steps, endpoint = True)
    elif scale == 'S':
        step_size = (max_val - min_val) / (steps - 1)
        vals = [(min_val + i*step_size) for i in range(steps)]
    else:
        raise ValueError('Unsupported scaling. Valid: L or S')    
else:
    raise ValueError('Unsupported operation. Valid: E or F')

results = {} #This list will store data of the different sumulations
report_info = {} #Dictionary with complete data for latex/pdf report
num_iter = 0
for value in vals:
    num_iter += 1
    
    #reset simulation to start over
    ns.sim_stop()
    ns.sim_reset()

    #If we are simulating with evolution we load the configuration parameters
    if steps > 1:
        print(f"Evolution iteration {num_iter}/{steps} Parameter value {value}")
        #value = min_val + sim*step_size
        
        #We work with a copy of the configuration
        iter_config = copy.deepcopy(config)
        
        #Update configuration object with each value to simulate with
        iter_config = load_config(iter_config, element, prop, value)
        #Check configuration file sintax
        validate_conf(iter_config)

    #Instantiate NetWorkManager based on configuration. Will launch routing protocol
    net = NetworkManager(iter_config)

    dc={}
    for path in net.get_paths():
        application = net.get_config('requests',path['request'],'application')
        if application == 'Capacity':
            app = CapacityApplication(path, net, f"CapacityApplication_{path['request']}")
        elif application == 'Teleportation':
            qubits = net.get_config('requests',path['request'],'teleport')
            epr_pair = net.get_config('epr_pair','epr_pair')
            app = TeleportationApplication(path, net, qubits, epr_pair, 'Teleportation', name = f"TeleportationApplication_{path['request']}")
        elif application == 'TeleportationWithDemand':
            qubits = net.get_config('requests',path['request'],'teleport')
            demand_rate = net.get_config('requests',path['request'],'demand_rate')
            epr_pair = net.get_config('epr_pair','epr_pair')
            app = TeleportationApplication(path, net, qubits, epr_pair, 'TeleportationWithDemand', rate=demand_rate, name=f"TeleportationWithDemandApplication_{path['request']}")
        elif application == 'QBER':
            qubits = net.get_config('requests',path['request'],'qber_states')
            epr_pair = net.get_config('epr_pair','epr_pair')
            app = TeleportationApplication(path, net, qubits, epr_pair, 'QBER', name = f"QBERApplication_{path['request']}")
        elif application == 'CHSH':
            app = CHSHApplication(path, net, name = f"CHSHApplication_{path['request']}")
        elif application == 'LogicalTeleportation':
            qubits = net.get_config('requests',path['request'],'teleport')
            epr_pair = net.get_config('epr_pair','epr_pair')
            app = TeleportationApplication(path, net, qubits, epr_pair, 'LogicalTeleportation', name = f"LogicalTeleportationApplication_{path['request']}")
        else:
            raise ValueError('Unsupported application')

        app.start()
        dc[path['request']] = [application, app.dc]

    #Run simulation
    duration = net.get_config('simulation_duration','simulation_duration')
    ns.sim_run(duration=duration)

    #Store simulation information for final report
    report_info[value] = net.get_info_report()

    print('----------------')

    #Acumulate results in general dataframe in case we want evolution
    for key,detail in dc.items():
        if detail[0] == 'Capacity':
            sim_result = {'Application':detail[0],
                          'Request': key,
                            'Parameter': element + '$' + prop, 
                            'Value': value,
                            'Generated Entanglements': len(detail[1].dataframe),
                            'Mean Fidelity': 0 if len(detail[1].dataframe) == 0 else detail[1].dataframe['Fidelity'].mean(),
                            'STD Fidelity': 0 if len(detail[1].dataframe) == 0 else detail[1].dataframe['Fidelity'].std(),
                            'Mean Time': 0 if len(detail[1].dataframe) == 0 else detail[1].dataframe['time'].mean(),
                            'STD Time': 0 if len(detail[1].dataframe) == 0 else detail[1].dataframe['time'].std(),
                            'Generation Rate': 0 if len(detail[1].dataframe) == 0 else 1e9*len(detail[1].dataframe)/float(config['simulation_duration'])
                            }
        elif detail[0] == 'Teleportation':
            sim_result = {'Application':detail[0],
                          'Request': key,
                            'Parameter': element + '$' + prop, 
                            'Value': value,
                            'Teleported States': len(detail[1].dataframe),
                            'Mean Fidelity': 0 if len(detail[1].dataframe) == 0 else detail[1].dataframe['Fidelity'].mean(),
                            'STD Fidelity': 0 if len(detail[1].dataframe) == 0 else detail[1].dataframe['Fidelity'].std(),
                            'Mean Time': 0 if len(detail[1].dataframe) == 0 else detail[1].dataframe['time'].mean(),
                            'STD Time': 0 if len(detail[1].dataframe) == 0 else detail[1].dataframe['time'].std()
                            }
        elif detail[0] == 'QBER':
            ok = 0 if len(detail[1].dataframe) == 0 else detail[1].dataframe['error'].value_counts().loc[0]
            total = 0 if len(detail[1].dataframe) == 0 else detail[1].dataframe['error'].count()
            sim_result = {'Application':detail[0],
                          'Request': key,
                            'Parameter': element + '$' + prop, 
                            'Value': value,
                            'Performed Measurements': len(detail[1].dataframe),
                            'Mean Time': 0 if len(detail[1].dataframe) == 0 else detail[1].dataframe['time'].mean(),
                            'STD Time': 0 if len(detail[1].dataframe) == 0 else detail[1].dataframe['time'].std(),
                            'QBER': 100 if len(detail[1].dataframe) == 0 else (total - ok) / total
                            }
        elif detail[0] == 'TeleportationWithDemand':
            nodename = net.get_config('requests',key,'origin')
            node = net.network.get_node(nodename)
            #queue_size = node.get_queue_size()
            sim_result = {'Application':detail[0],
                          'Request': key,
                            'Parameter': element + '$' + prop, 
                            'Value': value,
                            'Teleported States': len(detail[1].dataframe),
                            'Mean Fidelity': 0 if len(detail[1].dataframe) == 0 else detail[1].dataframe['Fidelity'].mean(),
                            'STD Fidelity': 0 if len(detail[1].dataframe) == 0 else detail[1].dataframe['Fidelity'].std(),
                            'Mean Time': 0 if len(detail[1].dataframe) == 0 else detail[1].dataframe['time'].mean(),
                            'STD Time': 0 if len(detail[1].dataframe) == 0 else detail[1].dataframe['time'].std(),
                            'Queue Size': 0 if len(detail[1].dataframe) == 0 else detail[1].dataframe['queue_size'].max(),
                            'Discarded Qubits': 0 if len(detail[1].dataframe) == 0 else detail[1].dataframe['discarded_qubits'].max()
                            }
        elif application == 'CHSH':
            wins = 0 if len(detail[1].dataframe) == 0 else len(detail[1].dataframe[detail[1].dataframe['wins']==1])
            total = 0 if len(detail[1].dataframe) == 0 else detail[1].dataframe['wins'].count()
            sim_result = {'Application':detail[0],
                          'Request': key,
                            'Parameter': element + '$' + prop, 
                            'Value': value,
                            'Measurements': len(detail[1].dataframe),
                            'Wins': 0 if total == 0 else (wins)/total,
                            'Mean Time': 0 if len(detail[1].dataframe) == 0 else detail[1].dataframe['time'].mean(),
                            'STD Time': 0 if len(detail[1].dataframe) == 0 else detail[1].dataframe['time'].std(),
                            }
        elif detail[0] == 'LogicalTeleportation':
            sim_result = {'Application':detail[0],
                          'Request': key,
                            'Parameter': element + '$' + prop, 
                            'Value': value,
                            'Teleported States': len(detail[1].dataframe),
                            'Mean Fidelity': 0 if len(detail[1].dataframe) == 0 else detail[1].dataframe['Fidelity'].mean(),
                            'STD Fidelity': 0 if len(detail[1].dataframe) == 0 else detail[1].dataframe['Fidelity'].std(),
                            'Mean Time': 0 if len(detail[1].dataframe) == 0 else detail[1].dataframe['time'].mean(),
                            'STD Time': 0 if len(detail[1].dataframe) == 0 else detail[1].dataframe['time'].std()
                            }
        else:
            raise ValueError('Unsupported application')

        if key not in results.keys(): results[key] = [] #Initialize list
        results[key].append(sim_result)

#Al this point in results we have the simulation data
simulation_data = {}
for key in results.keys():
    df_sim_result = pd.DataFrame(results[key])
    simulation_data[key] = df_sim_result

#Print results, we use current time.
# results will store simulation results, routing the routing calculation parameters
# and def the configured parameters
results_file = './output/results_' + datetime.datetime.now().strftime('%Y-%m-%d_%H-%M-%S') + '.csv'
routing_file = './output/routing_' + datetime.datetime.now().strftime('%Y-%m-%d_%H-%M-%S') + '.csv'
def_file = './output/definitionfile_' + datetime.datetime.now().strftime('%Y-%m-%d_%H-%M-%S') + '.txt'
try:
    os.remove(results_file)
    os.remove(routing_file)
    os.remove(def_file)
except:
    pass

for key,value in simulation_data.items():
    print(f"----Request {key}: Application: {value.iloc[0]['Application']} --------------------------------------")
    if value.iloc[0]['Application'] == 'Capacity':
        print(f"         Generated entanglements: {value['Generated Entanglements'].tolist()}")
        print(f"         Mean fidelity: {value['Mean Fidelity'].tolist()}")
        print(f"         STD fidelity: {value['STD Fidelity'].tolist()}")
        print(f"         Mean time: {value['Mean Time'].tolist()} nanoseconds")
        print(f"         STD time: {value['STD Time'].tolist()} nanoseconds")
        print(f"Entanglement generation rate: {value['Generation Rate'].tolist()} entanglements per second")    
    elif value.iloc[0]['Application'] == 'Teleportation':
        print(f"         Teleported states: {value['Teleported States'].tolist()}")
        print(f"         Mean fidelity: {value['Mean Fidelity'].tolist()}")
        print(f"         STD fidelity: {value['STD Fidelity'].tolist()}")
        print(f"         Mean time: {value['Mean Time'].tolist()} nanoseconds")
        print(f"         STD time: {value['STD Time'].tolist()} nanoseconds")
    elif value.iloc[0]['Application'] == 'QBER':
        print(f"         Performed measurements: {value['Performed Measurements'].tolist()}")
        print(f"         Mean time: {value['Mean Time'].tolist()} nanoseconds")
        print(f"         STD time: {value['STD Time'].tolist()} nanoseconds")
        print(f"QBER: {value['QBER'].tolist()}%")
    elif value.iloc[0]['Application'] == 'TeleportationWithDemand':
        print(f"         Teleported states: {value['Teleported States'].tolist()}")
        print(f"         Mean fidelity: {value['Mean Fidelity'].tolist()}")
        print(f"         STD fidelity: {value['STD Fidelity'].tolist()}")
        print(f"         Mean time: {value['Mean Time'].tolist()} nanoseconds")
        print(f"         STD time: {value['STD Time'].tolist()} nanoseconds")
        print(f"Queue size at end of simulation: {value['Queue Size'].tolist()}")
        print(f"Discarded qubits: {value['Discarded Qubits'].tolist()}")
    elif value.iloc[0]['Application'] == 'CHSH':
        print(f"        Measurements: {value['Measurements'].tolist()}")
        print(f"         Mean time: {value['Mean Time'].tolist()} nanoseconds")
        print(f"         STD time: {value['STD Time'].tolist()} nanoseconds")
        print(f"Wins: {value['Wins'].tolist()}")
    elif value.iloc[0]['Application'] == 'LogicalTeleportation':
        print(f"         Logical Teleported states: {value['Teleported States'].tolist()}")
        print(f"         Mean fidelity: {value['Mean Fidelity'].tolist()}")
        print(f"         STD fidelity: {value['STD Fidelity'].tolist()}")
        print(f"         Mean time: {value['Mean Time'].tolist()} nanoseconds")
        print(f"         STD time: {value['STD Time'].tolist()} nanoseconds")
    print()
    #If evolution, plot graphs
    if mode == 'E': create_plot(value,key,value.iloc[0]['Application'])

    #Save data to disk
    value.to_csv(results_file, mode='a', index=False, header=False)

#Store definition file
with open(def_file,'w') as deffile:
    if mode == 'F':
        deffile.write('Execution in Fixed mode\n--------------------------\n')
    else:
        deffile.write(f'Execution in Evolution mode\nElement:{element}\nParameter:{prop}\nMinimum value:{min_val}\nMaximum value:{max_val}\nSteps:{steps}\n---------------\n')
    yaml.dump(config, deffile, default_flow_style=False)

#Store routing calculations
with open(routing_file, 'a') as route_file:
    route_file.write('----------Link fidelities------------\n')
    route_file.write('param_value;link;cost;fidelity;num_metrics\n')
    for key, value in report_info.items():
        for link, fids in value['link_fidelities'].items():
            route_file.write(f"{key};{link};{fids[0]};{fids[1]};{fids[2]}\n")
    route_file.write('----------Requests status-----------\n')
    route_file.write('param_value;request;fidelity;purif_rounds;time;result;reason;shortest_path\n')
    for key, value in report_info.items():
        for data in value['requests_status']:
            route_file.write(f"{key};{data['request']};{data['fidelity']};{data['purif_rounds']};{data['time']};{data['result']};{data['reason']};{data['shortest_path']}\n")    

with open(results_file,'a') as resultsfile:
    resultsfile.write('\n---------Column values-------\n')
    resultsfile.write('Capacity;Request;Element$Parameter;Value;Generated Entanglements;Mean fidelity;STD fidelity;Mean time;STD time;Entanglement Generation rate;\n')
    resultsfile.write('Teleportation;Request;Element$Parameter;Value;Teleported states;Mean fidelity;STD fidelity;Mean time;STD time;\n')
    resultsfile.write('QBER;Request;Element$Parameter;Value;Performed measurements;Mean time;STD time;\n')
    resultsfile.write('TeleportationWithDemand;Request;Element$Parameter;Value;Teleported states;Mean fidelity;STD fidelity;Mean time;STD time;Queue size at end of simulation;Discarded qubits;\n')
    resultsfile.write('Teleportation;Request;Element$Parameter;Value;Measurements;Mean time;STD time;Wins;\n')

if print_report: 
    simul_environ = {
        'mode': mode,
        'element': element,
        'parameter': prop,
        'min_value': min_val,
        'max_value': max_val,
        'steps': steps,
        'def_file': def_file,
        'routing_file': routing_file,
        'results_file': results_file
    }
    generate_report(report_info, simulation_data, simul_environ)
