# agy with a placeholder token and a custom CA (free, local)

The Substrate rung's credential path rests on three things agy has to do, checked here against a
fake Google front end on an internal Docker network (no internet, no real token, no model call):

1. take its token from a metadata server named by `GCE_METADATA_HOST` (host or host:port), so a
   shim inside the actor can hand it a placeholder;
2. trust a CA given by `SSL_CERT_FILE`, as Substrate's MITM egress gateway requires;
3. reach its model call with only `aiplatform.googleapis.com` resolvable.

Make a throwaway CA and a leaf for `*.googleapis.com` in `ca/` and `srv/` (valid one day; never reuse them):

    mkdir -p ca srv
    openssl req -x509 -newkey rsa:2048 -nodes -days 1 -subj "/CN=lab throwaway CA" \
        -keyout ca/ca.key -out ca/ca.pem
    openssl req -newkey rsa:2048 -nodes -subj "/CN=*.googleapis.com" -keyout srv/leaf.key -out srv/leaf.csr
    openssl x509 -req -days 1 -in srv/leaf.csr -CA ca/ca.pem -CAkey ca/ca.key -CAcreateserial \
        -extfile <(printf "subjectAltName=DNS:*.googleapis.com") -out srv/leaf.pem

Then:

    bash run.sh -e SSL_CERT_FILE=/run/ate/egress-trust-bundle/ca.pem             # all hosts
    bash run.sh                                                                     # no CA: must fail
    HOSTS=aiplatform.googleapis.com bash run.sh -e SSL_CERT_FILE=...                # Vertex only
    HOSTS=aiplatform.googleapis.com META_ENV="-e GCE_METADATA_HOST=metadata.google.internal:8081" \
      bash run.sh -e SSL_CERT_FILE=...                                              # host:port

The front end logs each request's Host, path and the first 40 characters of Authorization, and
answers 401. Pass is `Bearer ate-placeholder` reaching `aiplatform.googleapis.com`.
