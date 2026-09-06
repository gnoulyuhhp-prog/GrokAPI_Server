package main

import (
	"fmt"
	"github.com/Wei-Shaw/sub2api/internal/config"
)

func main() {
	cfg, err := config.Load()
	if err != nil {
		fmt.Println("Error:", err)
		return
	}
	fmt.Println("PasswordAuthEnabled:", cfg.Gateway.Grok.PasswordAuthEnabled)
}