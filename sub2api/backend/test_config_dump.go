package main

import (
	"encoding/json"
	"fmt"
	"github.com/Wei-Shaw/sub2api/internal/config"
)

func main() {
	cfg, err := config.Load()
	if err != nil {
		fmt.Println("Error:", err)
		return
	}
	b, _ := json.MarshalIndent(cfg.Gateway.Grok, "", "  ")
	fmt.Println(string(b))
}