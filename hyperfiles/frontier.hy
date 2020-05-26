.IMPORT
CTR none
API (cnf)-->solution
BUILD
TENSOR
LEDGER
.PKG0
:@hash256@
:http:\\www.docker.com\ubuntu:18.0.oci
:http:\\www.ocirepo.rethat.com\ubuntu:18.0.oci
:@AfterAll
.PKG1
:@BeforeAll
:@hash256@
:apt-get install python3.8
:@AfterAll
TENSOR
.PKG2
:@BeforeAll
:@hash256@
:git clone https:\\www.github.com\josemibnf\sat-solver\frontier1.py
:npm install josemibnf.frontier1
:@AfterAll