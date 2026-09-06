package main

import (
	"fmt"
	"github.com/Wei-Shaw/sub2api/internal/config"
	"github.com/spf13/viper"
)

func main() {
	_, err := config.Load()
	if err != nil {
		fmt.Println("Error:", err)
		return
	}
	fmt.Println("Config used:", viper.ConfigFileUsed())
}