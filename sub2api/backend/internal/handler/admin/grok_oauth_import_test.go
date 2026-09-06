package admin

import "testing"

func TestNormalizeSSOImportTokensPreservesPasswordCharacters(t *testing.T) {
	input := "owner@example.com| leading,password;with|pipes "
	got := normalizeSSOImportTokens([]string{input}, "")
	if len(got) != 1 {
		t.Fatalf("expected one entry, got %d", len(got))
	}
	if got[0] != input {
		t.Fatalf("password entry changed: got %q, want %q", got[0], input)
	}
}

func TestNormalizeSSOImportTokensSupportsLegacySeparatorAndDedupes(t *testing.T) {
	input := "owner@example.com----password-with-dashes"
	got := normalizeSSOImportTokens([]string{input, input}, "")
	if len(got) != 1 || got[0] != "owner@example.com|password-with-dashes" {
		t.Fatalf("unexpected normalized entries: %#v", got)
	}
}

func TestParseGrokPasswordImportRejectsSSOTokens(t *testing.T) {
	if _, _, ok := parseGrokPasswordImport("sso-example-token"); ok {
		t.Fatal("SSO token was misclassified as a password entry")
	}
}

func TestNormalizeGrokImportConcurrency(t *testing.T) {
	for _, tc := range []struct{ input, want int }{{0, 3}, {-1, 3}, {5, 5}, {99, 10}} {
		if got := normalizeGrokImportConcurrency(tc.input); got != tc.want {
			t.Fatalf("normalizeGrokImportConcurrency(%d) = %d, want %d", tc.input, got, tc.want)
		}
	}
}
