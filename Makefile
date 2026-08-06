.PHONY: bootstrap generate test test-go test-agent run-infra up up-wecom down logs upload-example

bootstrap:
	mkdir -p .bin
	GOBIN=$(CURDIR)/.bin go install google.golang.org/protobuf/cmd/protoc-gen-go@latest
	GOBIN=$(CURDIR)/.bin go install google.golang.org/grpc/cmd/protoc-gen-go-grpc@latest
	uv sync --project agent
	go mod tidy
	$(MAKE) generate

generate:
	PATH=$(CURDIR)/.bin:$$PATH uv run --project agent python -m grpc_tools.protoc \
		-Iproto \
		--python_out=agent/src \
		--grpc_python_out=agent/src \
		--go_out=. --go_opt=module=workchat_bot \
		--go-grpc_out=. --go-grpc_opt=module=workchat_bot \
		proto/agent/v1/agent.proto

test: test-go test-agent

test-go:
	go test ./...

test-agent:
	uv run --project agent ruff check agent/src agent/tests tests/integration
	uv run --project agent ruff format --check agent/src agent/tests tests/integration
	uv run --project agent pytest -q

run-infra:
	docker compose up -d postgres redis minio

up:
	docker compose up -d --build postgres redis minio agent-grpc agent-http gateway dispatcher

up-wecom:
	docker compose --profile wecom up -d --build \
		postgres redis minio agent-grpc agent-http gateway dispatcher outbound

down:
	docker compose down

logs:
	docker compose logs -f --tail=200

upload-example:
	curl --fail-with-body -X POST http://127.0.0.1:8081/internal/v1/documents \
		-H "X-Internal-Token: $${AGENT_ADMIN_TOKEN:-local-dev-token}" \
		-F "knowledge_base_id=$${KNOWLEDGE_BASE_ID:-00000000-0000-0000-0000-000000000101}" \
		-F "source_code=example-customer-opening" \
		-F "file=@examples/knowledge/customer-opening.md;type=text/markdown"
