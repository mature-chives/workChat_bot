package outbound

import (
	"strings"
	"testing"
	"unicode/utf8"

	"workchat_bot/internal/store"
)

func TestFormatWeComTextIncludesCitations(t *testing.T) {
	text := FormatWeComText(store.OutboundAnswer{
		Content: "开户需要提交身份证明。 [1]",
		Citations: []store.OutboundCitation{
			{Index: 1, Title: "客户开户指引", LocatorType: "SECTION", LocatorValue: "开户资料"},
		},
	})

	if !strings.Contains(text, "来源：\n[1]《客户开户指引》 SECTION 开户资料") {
		t.Fatalf("FormatWeComText() = %q", text)
	}
}

func TestFormatWeComTextTruncatesAtUTF8Boundary(t *testing.T) {
	text := FormatWeComText(store.OutboundAnswer{Content: strings.Repeat("企", 1000)})

	if len(text) > maxWeComTextBytes {
		t.Fatalf("formatted text has %d bytes, limit is %d", len(text), maxWeComTextBytes)
	}
	if !utf8.ValidString(text) {
		t.Fatal("formatted text is not valid UTF-8")
	}
}
