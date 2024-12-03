Execution
----------
Place the source code files in the desired location and execute main.py through python interpreter.
Example:
.. code-block:: shell
   python3 main.py

Network topology and parameters must be placed in a file named **network_config.yaml** in the same directory. Some examples are documented in the [examples](../examples/) directory.

A directory named **output** must be created before execution.

Execution modes
----------------
Once executed, the program will ask for the execution mode
.. code-block:: shell
   Do you want to perform fixed parameter simulation or evolution? (F: Fixed, E: Evolution):

Values can be:
- Fixed: Simulator will get the network topology and all component parameters from the configuration file.
- Evolution: Topology will be read from the configuration file. All parameters are loaded from the file exept for one of them. Several simulations will be executed, each one of them with a different value of the specified parameter.

