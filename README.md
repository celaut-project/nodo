1. Create hy user and make it sudo

2. git clone https://github.com/josemibnf/HyperNode.git node

3. sudo apt-get install python3 python3-pip

4. pip3 install -r requirements.txt

5. Install Docker https://docs.docker.com/engine/install/ubuntu/

6. Docker without sudo https://docs.docker.com/engine/install/linux-postinstall/

7. systemd gateway.service

8. create the file on /etc/systemd/system/
https://docs.google.com/document/d/1VZ_M9mVKDe2VMsmMyHZAqedgWYrIuVEgqxc2t3-5UzM/edit

9. systemctl start gateway.service

10. systemctl enable gateway.service

11. Enable MongoDB https://www.digitalocean.com/community/tutorials/how-to-install-mongodb-on-ubuntu-20-04-es

12. Añadir emuladores de otra arquitectura. (Docker la detecta por si solo por lo que no es necesario crear un fork del nodo).
https://www.stereolabs.com/docs/docker/building-arm-container-on-x86/

13. Optional: activate ssh, https://linuxize.com/post/how-to-enable-ssh-on-ubuntu-18-04/
