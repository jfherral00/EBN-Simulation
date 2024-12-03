Execution
----------
Place the source code files in the desired location and execute main.py through python interpreter.
Example:
.. code-block:: shell
   python3 main.py

Network topology and parameters must be placed in a file named **network_config.yaml** in the same directory. Some examples are documented in the **examples** directory.

A directory named **output** must be created before execution.

Execution modes
----------------
Once executed, the program will ask for the execution mode
.. code-block:: shell
   Do you want to perform fixed parameter simulation or evolution? (F: Fixed, E: Evolution):

Values can be:

- Fixed: Simulator will get the network topology and all component parameters from the configuration file.
- Evolution: Topology will be read from the configuration file. All parameters are loaded from the file exept for one of them. Several simulations will be executed, each one of them with a different value of the specified parameter.

In Fixed mode execution will start and results stored in the **output** directory.

In Evolution mode, several additional parameters are requested:
.. code-block:: shell
   Do you want to perform fixed parameter simulation or evolution? (F: Fixed, E: Evolution): E
   Enter object (nodes/links/requests). Parameter will be set in ALL instances: nodes
   Enter property: gate_duration
   Enter minimum value: 1000
   Enter maximum value: 25000
   Enter number of steps (minimum 2): 10
   Do you want data points in (L)og scale or equally (S)paced? (L/S)S

- object: the structure the parameter is associated with: nodes, links or demands.
- property: the name of the property that will be varying.
- minimum value: start value of the property.
- maximum value: end value.
- number of steps: the number of simulations that will be run. Each simulation will be executed with a parameter value ranging from the start value to the maximum one.
- data point scale: wether the values will be generated linearly or in a logarithmic scale.

Results
---------------
Results will be printed in console and some files are stored in the **output** directory:

- definitionfile_<YYYY-MM-DD>_<HH:mm:ss>.txt: yaml file that was used for the simulation. Date and time are appended.
- results_<YYYY-MM-DD>_<HH:mm:ss>.csv: file in .csv format with the resulting metrics of the simulation. Date and time are appended.
- routing_<YYYY-MM-DD>_<HH:mm:ss>.txt: csv file with the routing metrics used by the network hipervisor in order to calculate paths. Date and time are appended.
-report.tex and report.pdf: report in PDF and latex format summarizing the simulation. If execution mode was *Evolution* graphs will be included.
