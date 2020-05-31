package node

func main() {
	var file = "hyperfiles/frontier.hy"
	if validHyperfile(file) {
		var image = makeImage(file)
		show(image)
		build(image)
	}
}
