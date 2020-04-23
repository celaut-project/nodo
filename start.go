package main

import (
	"fmt"
	"os"
)

func getthebest(tensor string) {
	fmt.Printf("Founding way to resolve ...  %#v\n", tensor)
	// Open our jsonFile
	jsonFile, err := os.Open("data.json")
	// if we os.Open returns an error then handle it
	if err != nil {
		fmt.Println(err)
	}
	fmt.Println("Successfully Opened data.json")
	// defer the closing of our jsonFile so that we can parse it later on
	defer jsonFile.Close()
}

func main() {
	fmt.Print("Starting node\n")
	var tensor string
	fmt.Print("Select the problem.. tensor:  ")
	fmt.Scanf("%s", &tensor)
	getthebest(tensor)
}
