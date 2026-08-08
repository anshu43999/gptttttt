package replay

import (
	"bytes"
	"fmt"
	"io"
	"net/http"
)

const (
	causalLaneCapture      = "capture"
	causalLaneRegistration = "registration"
	causalLaneSentinel     = "sentinel"
)

type compiledExchange struct {
	position        Position
	captureSequence int
	causalLane      string
	dependencies    []int
	request         compiledRequest
	response        compiledResponse
}

type compiledRequest struct {
	method                 string
	host                   string
	path                   string
	contentType            string
	query                   compiledCollection
	body                    compiledBody
	headers                 map[string]compiledHeaderRule
	allowUnspecifiedHeaders bool
	cookies                 []compiledCookieRule
	sentinelOccurrence      *int
	flowName                string
}

type compiledCollection struct {
	fields     map[string]compiledValueRule
	forbidden map[string]struct{}
	order      []string
	allowExtra bool
}

type compiledBody struct {
	kind       string
	fields     compiledCollection
	raw        compiledValueRule
	allowEmpty bool
}

type compiledHeaderRule struct {
	presence     string
	value        compiledValueRule
	multiplicity string
}

type compiledValueRule struct {
	kind       string
	literal    string
	slot       string
	objectKeys []string
	required   bool
}

type compiledCookieRule struct {
	name      string
	slot      string
	domain    string
	path      string
	httpOnly  bool
	secure    bool
	sameSite  http.SameSite
	required  bool
	allowSeed bool
}

type compiledResponse struct {
	statusCode      int
	replayable      bool
	outcome         string
	redirectMaxHops int
	header          http.Header
	body            []byte
}

func (r compiledResponse) clone(req *http.Request) *http.Response {
	header := make(http.Header, len(r.header))
	for key, values := range r.header {
		header[key] = append([]string(nil), values...)
	}
	statusText := http.StatusText(r.statusCode)
	status := ""
	if statusText != "" {
		status = fmt.Sprintf("%d %s", r.statusCode, statusText)
	}
	return &http.Response{
		Status:        status,
		StatusCode:    r.statusCode,
		Proto:         "HTTP/1.1",
		ProtoMajor:    1,
		ProtoMinor:    1,
		Header:        header,
		Body:          io.NopCloser(bytes.NewReader(r.body)),
		ContentLength: int64(len(r.body)),
		Request:       req,
	}
}
