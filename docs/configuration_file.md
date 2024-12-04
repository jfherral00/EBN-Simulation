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
- *type*: node type, endnode or switch 
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
- *teleport_queue_size*: transmission buffer size. Used when the application is *TeleportationWithDemand*
- *teleport_queue_technology*: transmission buffer memory technology. Can be *Quantum* or *Classical*. Used when the application is *TeleportationWithDemand*
- *teleport_strategy*: qubit selection stategy in the transmission buffer. Values: *Newest* (LIFO) or *Oldest* (FIFO). Used when the application is *TeleportationWithDemand*

Links
------
- *end1*: first end point in the link. Must match the name of a node 
- *end2*: second end point in the link. Must match the name of a node
- *distance*: distance in km of the link connecting the nodes
- *number_links*: number of links between the nodes. Only applies for links between switches
- *source_fidelty_sq*: probability of a perfect Bell pair being generated at the source associated to the link 
- *source_delay*: time (nanoseconds) that the quantum source needs to emit the EPR once triggered
- *photon_speed_fibre*: speed of the photon in km/s
- *qchannel_noise_model*: noise model that the quantum channel follows. Allowed values: DephaseNoiseModel, DepolarNoiseModel, T1T2NoiseModel, FibreDepolarizeModel, FibreDepolGaussModel, None
- *p_depol_init*: to be used when quantum noise model is FibreDepolarizeModel. Probability of the photon being depolarized when being transferred from the quantum memory to the channel.
- *p_depol_length*: to be used when quantum noise model is FibreDepolarizeModel. Probability of the photon being depolarized per channel kilometer 
- *dephase_qchannel_rate*: To be used for DephaseNoiseModel. Dephasing rate (hz)
- *depolar_qchannel_rate*: To be used for DepolarNoiseModel. Depolarizing rate (hz)
- *t1_qchannel_time*: T1 duration (nanoseconds). Only for T1T2NoiseModel quantum noise model
- *t2_qchannel_time*: T2 duration (nanoseconds). Only for T1T2NoiseModel quantum noise model
- *qchannel_loss_model*: loss model that que quantum channel follows. Allowed values: FibreLossModel, None
- *p_loss_init*: to be used when quantum loss model is FibreLossModel. Probability of the photon being lost when transferred from the quantum memory to the channel.
- *p_loss_length*: to be used when quantum loss model is FibreLossModel. Probability of the photon being lost per channel kilometer 
- *classical_delay_model*: Delay model for classical channels. Allowed values: FibreDelayModel or GaussianDelayModel
- *gaussian_delay_mean*: mean value of the gaussian distribution
- *gaussian_delay_std*: standard deviation of the gaussian distribution


Requests (demands)
-----------
- *origin*: node that will be the origin in the application. Must match the name of an end node
- *destination*: node that will be the destination in the application. Must mathc the name of an end node
- *minfidelity*: minimum fidelity requested by the demand
- *maxtime*: maximum entanglement generation time (nanoseconds)
- *path_fidel_rounds*: number of simulations to be executed when estimating the end to end fidelity. If defined, will override the general parameter for this request
- *application*: quantum application to be executed. Allowed values: Capacity, Teleportation, TeleportationWithDemand, QBER, CHSH, LogicalTeleportation
- *teleport*: list of qubits to be teleported. Used with teleportation applications
- *demand_rate*: qubit generation uniform rate (hz). Used with TeleportationWithDemand application
- *qber_states*: quantum states to be used for QBER application 
