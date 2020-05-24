package node

func makePod(){
	file = getFile()
	pod = readFile(file)
	buildPod(pod)
}

func main()  {
	makePod()
}