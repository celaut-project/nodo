docker stop $(docker ps -q)
docker system prune -a
rm -rf __registry__
rm -rf __cache__
systemctl restart docker.service