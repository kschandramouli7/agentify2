package api

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"time"
)

// AgentClient communicates with the Python agent service.
type AgentClient struct {
	baseURL string
	client  *http.Client
}

// NewAgentClient creates a new agent client.
func NewAgentClient(baseURL string) *AgentClient {
	return &AgentClient{
		baseURL: baseURL,
		client: &http.Client{
			Timeout: 180 * time.Second, // Opus 4.8 takes 60-90s; frontend waits 190s
		},
	}
}

// AgentRequest is sent to the agent service.
type AgentRequest struct {
	Intent  string                 `json:"intent"`
	Data    map[string]interface{} `json:"data"`
	Context map[string]interface{} `json:"context"`
	TraceID string                 `json:"trace_id,omitempty"` // propagated for cross-service correlation (spec 004)
}

// AgentToolCall is a tool the agent invoked (for provenance).
type AgentToolCall struct {
	Name      string                 `json:"name"`
	Arguments map[string]interface{} `json:"arguments"`
}

// AgentResponse is returned from the agent service.
type AgentResponse struct {
	Answer                   string                 `json:"answer"`
	Status                   string                 `json:"status"` // "healthy","degraded","unhealthy","error","unknown"
	Reasoning                string                 `json:"reasoning"`
	Confidence               float64                `json:"confidence"`
	Sources                  []string               `json:"sources"`
	ToolCalls                []AgentToolCall        `json:"tool_calls"`
	Details                  map[string]interface{} `json:"details"` // severity, likely_cause, recommendations, findings (spec 005)
	InputTokens              int64                  `json:"input_tokens"`
	OutputTokens             int64                  `json:"output_tokens"`
	CacheCreationInputTokens int64                  `json:"cache_creation_input_tokens"`
	CacheReadInputTokens     int64                  `json:"cache_read_input_tokens"`
	EstimatedCostUSD         float64                `json:"estimated_cost_usd"`
	// Tier the agent actually used: "tier1" when it answered deterministically
	// with no model call. Empty for every response that predates the field, so
	// callers must default it rather than treat "" as a tier.
	Tier                     string                 `json:"tier"`
	PromptName               string                 `json:"prompt_name"`
	PromptVersion            *int                   `json:"prompt_version"`
}

// ChatRequest is sent to the agent's /reason-chat endpoint for multi-turn conversation.
type ChatRequest struct {
	Messages []map[string]string    `json:"messages"` // [{role, content}, ...]
	Context  map[string]interface{} `json:"context"`
	TraceID  string                 `json:"trace_id,omitempty"`
}

// Chat calls the agent for a multi-turn conversational response.
// messages should be the full conversation history in [{role, content}] format.
func (ac *AgentClient) Chat(messages []map[string]string, context map[string]interface{}, traceID string) (*AgentResponse, error) {
	req := ChatRequest{Messages: messages, Context: context, TraceID: traceID}
	body, err := json.Marshal(req)
	if err != nil {
		return nil, fmt.Errorf("failed to marshal chat request: %w", err)
	}
	httpReq, err := http.NewRequest("POST", ac.baseURL+"/reason-chat", bytes.NewReader(body))
	if err != nil {
		return nil, fmt.Errorf("failed to create chat request: %w", err)
	}
	httpReq.Header.Set("Content-Type", "application/json")
	resp, err := ac.client.Do(httpReq)
	if err != nil {
		return nil, fmt.Errorf("chat request failed: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		body, _ := io.ReadAll(resp.Body)
		return nil, fmt.Errorf("agent returned %d: %s", resp.StatusCode, string(body))
	}
	var agentResp AgentResponse
	if err := json.NewDecoder(resp.Body).Decode(&agentResp); err != nil {
		return nil, fmt.Errorf("failed to decode chat response: %w", err)
	}
	return &agentResp, nil
}

// LiveToolCallRequest is sent to the agent's /live-tool-call endpoint.
type LiveToolCallRequest struct {
	Tool      string                 `json:"tool"`
	Arguments map[string]interface{} `json:"arguments"`
}

// LiveToolCallResponse is returned from /live-tool-call.
type LiveToolCallResponse struct {
	Tool string                 `json:"tool"`
	Data map[string]interface{} `json:"data"`
}

// LiveToolCall directly invokes one of the agent's live-diagnostics tools —
// no LLM call, used by the Chat UI's "Run" buttons on a recommended action.
func (ac *AgentClient) LiveToolCall(tool string, arguments map[string]interface{}) (*LiveToolCallResponse, error) {
	body, err := json.Marshal(LiveToolCallRequest{Tool: tool, Arguments: arguments})
	if err != nil {
		return nil, fmt.Errorf("failed to marshal live tool call request: %w", err)
	}
	httpReq, err := http.NewRequest("POST", ac.baseURL+"/live-tool-call", bytes.NewReader(body))
	if err != nil {
		return nil, fmt.Errorf("failed to create live tool call request: %w", err)
	}
	httpReq.Header.Set("Content-Type", "application/json")
	resp, err := ac.client.Do(httpReq)
	if err != nil {
		return nil, fmt.Errorf("live tool call request failed: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		respBody, _ := io.ReadAll(resp.Body)
		return nil, fmt.Errorf("agent returned %d: %s", resp.StatusCode, string(respBody))
	}
	var out LiveToolCallResponse
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		return nil, fmt.Errorf("failed to decode live tool call response: %w", err)
	}
	return &out, nil
}

// Reason calls the agent service to reason about the data.
func (ac *AgentClient) Reason(question string, intent string, data map[string]interface{}, context map[string]interface{}, traceID string) (*AgentResponse, error) {
	req := AgentRequest{
		Intent:  intent,
		Data:    data,
		Context: context,
		TraceID: traceID,
	}

	body, err := json.Marshal(req)
	if err != nil {
		return nil, fmt.Errorf("failed to marshal request: %w", err)
	}

	httpReq, err := http.NewRequest("POST", ac.baseURL+"/reason", bytes.NewReader(body))
	if err != nil {
		return nil, fmt.Errorf("failed to create request: %w", err)
	}

	httpReq.Header.Set("Content-Type", "application/json")

	resp, err := ac.client.Do(httpReq)
	if err != nil {
		return nil, fmt.Errorf("request failed: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		body, _ := io.ReadAll(resp.Body)
		return nil, fmt.Errorf("agent service returned %d: %s", resp.StatusCode, string(body))
	}

	var agentResp AgentResponse
	if err := json.NewDecoder(resp.Body).Decode(&agentResp); err != nil {
		return nil, fmt.Errorf("failed to decode response: %w", err)
	}

	return &agentResp, nil
}
