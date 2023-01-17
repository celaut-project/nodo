1.git clone https://github.com/josemibnf/HyperNode.git node

2.sudo apt-get install python3 python3-pip

3.pip3 install -r requirements.txt

4.Install Docker https://docs.docker.com/engine/install/ubuntu/

5. if is wsl2 https://gist.github.com/djfdyuruiry/6720faa3f9fc59bfdf6284ee1f41f950

6.systemd gateway.service

7.create the file (is in other posit) on /etc/systemd/system/
https://docs.google.com/document/d/1VZ_M9mVKDe2VMsmMyHZAqedgWYrIuVEgqxc2t3-5UzM/edit

8.systemctl start gateway.service

9.systemctl enable gateway.service

10. https://www.digitalocean.com/community/tutorials/how-to-install-mongodb-on-ubuntu-20-04-es

11. Añadir base de datos 'peerInstances' a mongoDB.

12. Añadir emuladores de otra arquitectura. (Docker la detecta por si solo por lo que no es necesario crear un fork del nodo).
https://www.stereolabs.com/docs/docker/building-arm-container-on-x86/

13.Optional: activate ssh, https://linuxize.com/post/how-to-enable-ssh-on-ubuntu-18-04/

14. Add enviroment variables: 
- COMPUTE_POWER_RATE
- GATEWAY_PORT
- COST_OF_BUILD
- EXECUTION_BENEFIT
- MEMORY_LOGS
- BUILD_CONTAINER_MEMORY_SIZE_FACTOR
- WAIT_FOR_CONTAINER_TIME
- USE_PRINT
- IGNORE_FATHER_NETWORK_ON_SERVICE_BALANCER
- SEND_ONLY_HASHES_ASKING_COST
- DENEGATE_COST_REQUEST_IF_DONT_VE_THE_HASH
- COMPILER_MEMORY_SIZE_FACTOR
- ARM_SUPPORT
- X86_SUPPORT
- DEFAULT_INITIAL_GAS_AMOUNT_FACTOR
- USE_DEFAULT_INITIAL_GAS_AMOUNT_FACTOR
- DEFAULT_INITIAL_GAS_AMOUNT
- MANAGER_ITERATION_TIME
- COST_AVERAGE_VARIATION
- MEMORY_LIMIT_COST_FACTOR
- GAS_COST_FACTOR
- MODIFY_SERVICE_SYSTEM_RESOURCES_COST
- MIN_PEER_DEPOSIT
- INITIAL_PEER_DEPOSIT_FACTOR
- ALLOW_GAS_DEBT
- COMMUNICATION_ATTEMPTS_DELAY
- COMMUNICATION_ATTEMPTS
- MIN_SLOTS_OPEN_PER_PEER
- CLIENT_EXPIRATION_TIME
- CLIENT_MIN_GAS_AMOUNT_TO_RESET_EXPIRATION_TIME
- GENERAL_ATTEMPTS
- GENERAL_WAIT_TIME
- CONCURRENT_CONTAINER_CREATIONS
- DOCKER_CLIENT_TIMEOUT
- DOCKER_MAX_CONNECTIONS
- MONGODB
