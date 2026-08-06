package outbound

import (
	"fmt"
	"strings"
	"unicode/utf8"

	"workchat_bot/internal/store"
)

const maxWeComTextBytes = 2000

func FormatWeComText(answer store.OutboundAnswer) string {
	content := strings.TrimSpace(answer.Content)
	var sources strings.Builder
	if len(answer.Citations) > 0 {
		sources.WriteString("\n\n来源：")
		for _, citation := range answer.Citations {
			title := compact(citation.Title, 80)
			location := compact(
				strings.TrimSpace(citation.LocatorType+" "+citation.LocatorValue), 80,
			)
			sources.WriteString(fmt.Sprintf("\n[%d]《%s》", citation.Index, title))
			if location != "" {
				sources.WriteString(" ")
				sources.WriteString(location)
			}
		}
	}

	suffix := sources.String()
	if len(content)+len(suffix) <= maxWeComTextBytes {
		return content + suffix
	}
	if len(suffix) >= maxWeComTextBytes-4 {
		return truncateUTF8(content+suffix, maxWeComTextBytes)
	}
	answerLimit := maxWeComTextBytes - len(suffix) - len("…")
	return truncateUTF8(content, answerLimit) + "…" + suffix
}

func compact(value string, maxRunes int) string {
	value = strings.Join(strings.Fields(value), " ")
	if utf8.RuneCountInString(value) <= maxRunes {
		return value
	}
	return string([]rune(value)[:maxRunes-1]) + "…"
}

func truncateUTF8(value string, byteLimit int) string {
	if len(value) <= byteLimit {
		return value
	}
	if byteLimit <= 0 {
		return ""
	}
	used := 0
	for index, char := range value {
		size := utf8.RuneLen(char)
		if used+size > byteLimit {
			return value[:index]
		}
		used += size
	}
	return value
}
