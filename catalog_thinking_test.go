package main

import (
	"encoding/json"
	"slices"
	"testing"
)

func TestParseCatalogEntryThinkingConfig(t *testing.T) {
	cases := []struct {
		name    string
		raw     string
		efforts []string
		def     string
		offOK   bool
	}{
		{
			name:    "qwen style with disabled and no high",
			raw:     `{"key":"qmodel_38max","display_name":"Qwen3.8-Max","is_reasoning":true,"thinking_config":{"disabled":{},"enabled":{"is_default":true,"efforts":{"medium":{"is_default":true},"low":{},"xhigh":{}}}}}`,
			efforts: []string{"low", "medium", "xhigh"},
			def:     "medium",
			offOK:   true,
		},
		{
			name:    "deepseek style high and max",
			raw:     `{"key":"dmodel","display_name":"DeepSeek-V4-Pro","is_reasoning":true,"thinking_config":{"disabled":{},"enabled":{"efforts":{"max":{"is_default":true},"high":{}}}}}`,
			efforts: []string{"high", "max"},
			def:     "max",
			offOK:   true,
		},
		{
			name:    "glm style no disabled",
			raw:     `{"key":"gmodel","display_name":"GLM-5.3","thinking_config":{"enabled":{"efforts":{"high":{"is_default":true},"low":{},"max":{}}}}}`,
			efforts: []string{"low", "high", "max"},
			def:     "high",
			offOK:   false,
		},
		{
			name:    "explicit null disabled is ignored",
			raw:     `{"key":"x","thinking_config":{"disabled":null,"enabled":{"efforts":{"low":{}}}}}`,
			efforts: []string{"low"},
			def:     "",
			offOK:   false,
		},
		{
			name:    "no thinking config",
			raw:     `{"key":"y","display_name":"Plain"}`,
			efforts: nil,
			def:     "",
			offOK:   false,
		},
	}

	for _, tt := range cases {
		t.Run(tt.name, func(t *testing.T) {
			mc, err := parseCatalogEntry(json.RawMessage(tt.raw))
			if err != nil {
				t.Fatalf("parseCatalogEntry() error = %v", err)
			}
			if mc == nil {
				t.Fatal("parseCatalogEntry() returned nil model")
			}
			if !slices.Equal(mc.Efforts, tt.efforts) {
				t.Fatalf("Efforts = %v, want %v", mc.Efforts, tt.efforts)
			}
			if mc.DefaultEffort != tt.def {
				t.Fatalf("DefaultEffort = %q, want %q", mc.DefaultEffort, tt.def)
			}
			if mc.SupportsThinkingOff != tt.offOK {
				t.Fatalf("SupportsThinkingOff = %v, want %v", mc.SupportsThinkingOff, tt.offOK)
			}
		})
	}
}

func TestThinkingEffortOrderKeepsUnknownLevels(t *testing.T) {
	ordered, def := sortedThinkingEfforts(map[string]struct {
		IsDefault bool `json:"is_default"`
	}{
		"max":     {},
		"zzz_new": {},
		"low":     {},
		"high":    {IsDefault: true},
	})
	if !slices.Equal(ordered, []string{"low", "high", "max", "zzz_new"}) {
		t.Fatalf("ordered = %v", ordered)
	}
	if def != "high" {
		t.Fatalf("default = %q, want high", def)
	}
}
