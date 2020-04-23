package main

import (
	"encoding/json"
	"fmt"
	"io/ioutil"
	"os"
)

// Tensors struct
type Tensors struct {
	TENSORS []Tensor `json:"tensors"`
}

// Tensor struct
type Tensor struct {
	ID string `json:"id"`

	DEF string `json:"definition"`

	CONTAINERS []Container `json:"containers"`
}

// Container struct
type Container struct {
	ID string `json:"id"`

	SCORE int `json:"score"`

	GIT string `json:"git"`
}

func getthebest(tensor string) {
	fmt.Printf("Founding way to resolve ...  %#v\n", tensor)

	// Open our jsonFile
	jsonFile, err := os.Open("data.json")
	// if we os.Open returns an error then handle it
	if err != nil {
		fmt.Println(err)
	}
	fmt.Println("Successfully Opened data.json")

	// read our opened jsonFile as a byte array.
	byteValue, _ := ioutil.ReadAll(jsonFile)
	// initialize out Tensors array
	var tensors Tensors
	// we unmarshal our byteArray which contains our
	// jsonFile's content into 'users' which we defined above
	json.Unmarshal(byteValue, &tensors)

	for i := 0; i < len(tensors.TENSORS); i++ {
		if tensors.TENSORS[i].DEF == tensor {
			fmt.Printf("Ya tenemos nuestro tensor ..  %#v\n", tensor)
		}
	}

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
