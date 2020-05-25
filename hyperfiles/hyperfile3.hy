FROM 10 #python
PKG0 22 #frontier
IMPORT
CTR none
API none
BUILD
TENSOR
LEDGER

,python
;FROM scratch
;PKG0 15 #ubuntu
;PKG1 #python
;
;. #ubuntu
;:@BeforeAll
;:@hash256@
;:ºhttp:\\www.docker.com\ubuntu:18.0.oci
;:@AfterAll
;
;. #python
;:@BeforeAll
;:@hash256@
;:apt-get install python3.8
;:@AfterAll

. #frontier
:@BeforeAll
:@hash256@
:git clone https:\\www.github.com\josemibnf\sat-solver\frontier1.py
:npm install josemibnf.frontier1
:@AfterAll