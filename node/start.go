package node

func main() {
	var file = "hyperfiles/frontier.hy"
	if validHyperfile(file) {
		var image = makePod(file)
		image.show()
		image.build()
	}
}
