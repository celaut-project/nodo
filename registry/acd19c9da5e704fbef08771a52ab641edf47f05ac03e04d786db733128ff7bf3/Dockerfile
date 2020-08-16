FROM ubuntu
RUN apt-get update --fix-missing && apt-get -y install python3.6 && apt-get clean
RUN apt-get update --fix-missing && apt-get -y install python3-tk && apt-get clean
RUN apt-get update --fix-missing && apt-get -y install python3-pip && apt-get clean
RUN pip3 install requests
RUN mkdir /satrainer && cd /satrainer && apt-get update && apt-get -y install git && git clone --branch sinapi https://github.com/josemibnf/SATrainer.git && mv SATrainer/* . && rm -rf SATrainer  && rm -rf .git && chmod 777 start.sh
