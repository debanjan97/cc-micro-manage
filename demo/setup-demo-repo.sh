#!/usr/bin/env bash
# Creates the small Go project the demo GIF is recorded in (/tmp/acme-api).
set -e
rm -rf /tmp/acme-api && mkdir -p /tmp/acme-api && cd /tmp/acme-api
mkdir -p "$(dirname README.md)"
cat > README.md <<'EOF'
# acme-api
A small HTTP API for managing widgets: CRUD endpoints, JWT auth, Postgres storage.
Run `go run ./cmd/server` and hit http://localhost:8080/widgets.
EOF
mkdir -p "$(dirname cmd/server/main.go)"
cat > cmd/server/main.go <<'EOF'
package main

import (
	"log"
	"net/http"

	"github.com/acme/acme-api/internal/widgets"
	"github.com/go-chi/chi/v5"
)

func main() {
	r := chi.NewRouter()
	widgets.Mount(r)
	// TODO: graceful shutdown on SIGTERM
	log.Fatal(http.ListenAndServe(":8080", r))
}
EOF
mkdir -p "$(dirname go.mod)"
cat > go.mod <<'EOF'
module github.com/acme/acme-api

go 1.23

require (
	github.com/go-chi/chi/v5 v5.1.0
	github.com/golang-jwt/jwt/v5 v5.2.1
	github.com/jackc/pgx/v5 v5.7.1
	go.uber.org/zap v1.27.0
)
EOF
mkdir -p "$(dirname internal/auth/jwt.go)"
cat > internal/auth/jwt.go <<'EOF'
package auth

// TODO: rotate signing keys; this one never changes
var signingKey = []byte("dev-only-not-a-secret")

func Verify(token string) bool {
	// FIXME: accepts expired tokens
	return token != ""
}
EOF
mkdir -p "$(dirname internal/widgets/handler.go)"
cat > internal/widgets/handler.go <<'EOF'
package widgets

import (
	"encoding/json"
	"net/http"

	"github.com/go-chi/chi/v5"
)

func Mount(r chi.Router) {
	r.Get("/widgets", list)
	r.Post("/widgets", create)
}

func list(w http.ResponseWriter, r *http.Request) {
	// TODO: pagination — this returns every row
	json.NewEncoder(w).Encode(store.All())
}

func create(w http.ResponseWriter, r *http.Request) {
	var wd Widget
	// FIXME: no input validation, empty names are accepted
	json.NewDecoder(r.Body).Decode(&wd)
	store.Add(wd)
	w.WriteHeader(http.StatusCreated)
}
EOF
mkdir -p "$(dirname internal/widgets/store.go)"
cat > internal/widgets/store.go <<'EOF'
package widgets

import "sync"

type Widget struct {
	ID   int    `json:"id"`
	Name string `json:"name"`
}

// HACK: in-memory store until the Postgres layer lands
type memStore struct {
	mu   sync.Mutex
	rows []Widget
}

var store = &memStore{}

func (s *memStore) All() []Widget { s.mu.Lock(); defer s.mu.Unlock(); return s.rows }
func (s *memStore) Add(w Widget)  { s.mu.Lock(); defer s.mu.Unlock(); w.ID = len(s.rows) + 1; s.rows = append(s.rows, w) }
EOF
echo "demo repo ready at /tmp/acme-api"
