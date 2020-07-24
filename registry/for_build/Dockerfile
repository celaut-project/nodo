FROM ubuntu
RUN apt-get update --fix-missing && apt-get -y install python3.6 && apt-get clean
RUN apt-get update --fix-missing && apt-get -y install python3-tk && apt-get clean
RUN apt-get update --fix-missing && apt-get -y install python3-pip && apt-get clean
RUN pip3 install flask
RUN pip3 install pandas
RUN pip3 install numpy
RUN mkdir /frontier && cd /frontier && apt-get update --fix-missing && apt-get -y install git && apt-get clean && git clone --branch frontier https://github.com/josemibnf/Solvers.git && mv Solvers/* . && chmod 777 gitPkg.sh && ./gitPkg.sh && rm gitPkg.sh 
ENTRYPOINT python3 /frontier/start.py