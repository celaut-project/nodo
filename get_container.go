package main

import (
	"os/exec"
)

func temp() {
	cd := exec.Command("cd", "temp")
	err := cd.Run()
	if err != nil {
		mk := exec.Command("mkdir", "temp")
		err := mk.Run()
		if err != nil {
			print("something went wrong, mkdir temp failed")
		}
		temp()
	}
}

func clone(repo string) {
	temp()
	cmd := exec.Command("git", "clone", repo)
	err := cmd.Run()
	if err != nil {
		print("something went wrong, git clone failed")
	}
}
