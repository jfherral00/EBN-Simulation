Configuration file
===================
The configuration file must be named *network_config.yaml* and must be placed in the same directory as the Python source code.

Several sections must be configured:
- global: common to the simulation
- nodes: parameters describing the nodes (end nodes and switches) in the network
- links: links connecting nodes
- requests: demands that will be executing concurrently

Global
------
- *name*: name of the network
- *link_fidel_rounds*: number of simulations that will be performed in order to estimate link fidelity
- *path_fidel_rounds*: number of simulations that will be performed by the hypervisor in order to estimate end to end fidelity
- *epr_par*: EPR that the quantum sources will generate. Allowed values: PHI_PLUS or PSI_PLUS
- *simulation_duration*: duration in nanoseconds of the application simulation phase

Nodes
------
- *type*: node type: endnode or switch 
- *num_memories*: size of the memory in the node
- *gate_duration*: generic instruction duration in nanoseconds 
- *gate_duration_X*: if defined, overrides the value for an X gate (nanoseconds)
- *gate_duration_Z*: if defined, overrides the value for a Z gate (nanoseconds)
- *gate_duration_CX*: if defined, overrides the value for a CX gate (nanoseconds)
- *gate_duration_rotations*: if defined, overrides the value for rotation (nanoseconds)
- *measurements_duration*: if defined, overrides the value for measurements (nanoseconds)
- *gate_noise_model*: Noise model in quantum gates. Allowed values: DephaseNoiseModel, DepolarNoiseModel or T1T2NoiseModel
- *dephase_gate_rate*: Dephase rate to be used when gate noise model is DephaseNoiseModel (hz)
- *depolar_gate_rate*: Depolarizing rate to be used when gate noise model is DepolarNoiseModel (hz)
- *t1_gate_time* and *t2_gate_time*: T1 and T2 times to be used when gate noise model is T1T2NoiseModel (nanoseconds)
- *mem_noise_model*: Noise model in quantum memories. Allowed values: DephaseNoiseModel, DepolarNoiseModel or T1T2NoiseModel
- *dephase_mem_rate*: Dephase rate to be used when memory noise model is DephaseNoiseModel (hz)
- *depolar_mem_rate*: Depolarizing rate to be used when memory noise model is DepolarNoiseModel (hz)
- *t1_mem_mem* and *t2_gate_time*: T1 and T2 times to be used when memory noise model is T1T2NoiseModel (nanoseconds)
- *teleport_queue_size*: transmission buffer size. Used when the application is *TeleportationWithDemand"
- *teleport_queue_technology*: transmission buffer memory technology. Can be *Quantum* or *Classical*. Used when the application is *TeleportationWithDemand"
- *teleport_strategy*: qubit selection stategy in the transmission buffer size. Values: *Newest* (LIFO) or *Oldest* (FIFO). Used when the application is *TeleportationWithDemand"
- **: 
