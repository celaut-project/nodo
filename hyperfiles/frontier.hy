PKG0 11 #ubuntu
PKG1 17 #python
PKG2 23 #frontier
IMPORT
CTR none
API none
BUILD
TENSOR
LEDGER

. #ubuntu
:@hash256@
:http:\\www.docker.com\ubuntu:18.0.oci
:http:\\www.ocirepo.rethat.com\ubuntu:18.0.oci
:@AfterAll

. #python
:@BeforeAll
:@hash256@
:apt-get install python3.8
:@AfterAll

. #frontier
:@BeforeAll
:@hash256@
:git clone https:\\www.github.com\josemibnf\sat-solver\frontier1.py
:npm install josemibnf.frontier1
:@AfterAll