// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Autonomy®

/**
 * @file etherdog_link.c
 * @brief EtherDOG protocol client (control lines and data frames).
 */

#include "etherdog_link.h"

#include "cJSON.h"

#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <netinet/in.h>
#include <poll.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <unistd.h>

#define KIND_OUTPUTS 1
#define KIND_INPUTS 2
#define PROTOCOL_VERSION 1

/* --- little-endian field helpers ------------------------------------------------------- */

static void put_le(uint8_t *p, uint64_t v, int n)
{
    for (int i = 0; i < n; i++)
        p[i] = (uint8_t)(v >> (8 * i));
}

static uint64_t get_le(const uint8_t *p, int n)
{
    uint64_t v = 0;
    for (int i = n - 1; i >= 0; i--)
        v = (v << 8) | p[i];
    return v;
}

/* --- session file ---------------------------------------------------------------------- */

int edl_read_session(const char *path, edl_session_t *out, char *err, size_t err_size)
{
    memset(out, 0, sizeof(*out));
    FILE *fp = fopen(path, "r");
    if (fp == NULL) {
        snprintf(err, err_size, "cannot open EtherDOG session file %s: %s", path,
                 strerror(errno));
        return -1;
    }
    char text[2048];
    size_t n = fread(text, 1, sizeof(text) - 1, fp);
    fclose(fp);
    text[n] = '\0';

    cJSON *root = cJSON_Parse(text);
    const cJSON *disabled = root ? cJSON_GetObjectItemCaseSensitive(root, "disabled") : NULL;
    if (cJSON_IsString(disabled)) {
        snprintf(err, err_size, "EtherCAT is disabled: %s", disabled->valuestring);
        cJSON_Delete(root);
        return EDL_DISABLED;
    }
    const cJSON *control = root ? cJSON_GetObjectItemCaseSensitive(root, "control") : NULL;
    if (!cJSON_IsString(control)) {
        snprintf(err, err_size, "EtherDOG session file %s has no 'control' endpoint", path);
        cJSON_Delete(root);
        return -1;
    }
    snprintf(out->control, sizeof(out->control), "%s", control->valuestring);
    const cJSON *token = cJSON_GetObjectItemCaseSensitive(root, "token");
    if (cJSON_IsString(token))
        snprintf(out->token, sizeof(out->token), "%s", token->valuestring);
    const cJSON *data = cJSON_GetObjectItemCaseSensitive(root, "data");
    out->udp = cJSON_IsString(data) && strcmp(data->valuestring, "udp") == 0;
    const cJSON *busconfig = cJSON_GetObjectItemCaseSensitive(root, "busconfig");
    if (cJSON_IsString(busconfig) && strlen(busconfig->valuestring) < sizeof(out->busconfig))
        snprintf(out->busconfig, sizeof(out->busconfig), "%s", busconfig->valuestring);
    cJSON_Delete(root);
    return 0;
}

/* --- control connection ---------------------------------------------------------------- */

void edl_init(edl_link_t *link)
{
    memset(link, 0, sizeof(*link));
    link->ctl_fd = -1;
    link->data_fd = -1;
}

static int connect_spec(const char *spec, char *err, size_t err_size)
{
    if (strncmp(spec, "unix:", 5) == 0) {
        struct sockaddr_un addr;
        memset(&addr, 0, sizeof(addr));
        addr.sun_family = AF_UNIX;
        if (strlen(spec + 5) >= sizeof(addr.sun_path)) {
            snprintf(err, err_size, "control socket path too long");
            return -1;
        }
        snprintf(addr.sun_path, sizeof(addr.sun_path), "%s", spec + 5);
        int fd = socket(AF_UNIX, SOCK_STREAM, 0);
        if (fd >= 0 && connect(fd, (struct sockaddr *)&addr, sizeof(addr)) == 0)
            return fd;
        snprintf(err, err_size, "cannot reach EtherDOG at %s: %s", spec, strerror(errno));
        if (fd >= 0)
            close(fd);
        return -1;
    }
    if (strncmp(spec, "tcp:", 4) == 0) {
        char host[64];
        const char *rest = spec + 4;
        const char *colon = strrchr(rest, ':');
        if (colon == NULL || (size_t)(colon - rest) >= sizeof(host)) {
            snprintf(err, err_size, "invalid control endpoint %s", spec);
            return -1;
        }
        memcpy(host, rest, (size_t)(colon - rest));
        host[colon - rest] = '\0';
        struct sockaddr_in addr;
        memset(&addr, 0, sizeof(addr));
        addr.sin_family = AF_INET;
        addr.sin_port = htons((uint16_t)atoi(colon + 1));
        if (inet_pton(AF_INET, host, &addr.sin_addr) != 1) {
            snprintf(err, err_size, "invalid control endpoint %s", spec);
            return -1;
        }
        int fd = socket(AF_INET, SOCK_STREAM, 0);
        if (fd >= 0 && connect(fd, (struct sockaddr *)&addr, sizeof(addr)) == 0)
            return fd;
        snprintf(err, err_size, "cannot reach EtherDOG at %s: %s", spec, strerror(errno));
        if (fd >= 0)
            close(fd);
        return -1;
    }
    snprintf(err, err_size, "unsupported control endpoint %s", spec);
    return -1;
}

int edl_call(edl_link_t *link, const char *request, char **response, int timeout_ms)
{
    *response = NULL;
    if (link->ctl_fd < 0)
        return -1;
    size_t len = strlen(request);
    const char *p = request;
    size_t left = len;
    while (left > 0) {
        ssize_t n = send(link->ctl_fd, p, left, 0);
        if (n < 0) {
            if (errno == EINTR)
                continue;
            return -1;
        }
        p += n;
        left -= (size_t)n;
    }
    if (send(link->ctl_fd, "\n", 1, 0) != 1)
        return -1;

    for (;;) {
        char *nl = link->rlen > 0 ? memchr(link->rbuf, '\n', link->rlen) : NULL;
        if (nl != NULL) {
            size_t line = (size_t)(nl - link->rbuf);
            char *out = malloc(line + 1);
            if (out == NULL)
                return -1;
            memcpy(out, link->rbuf, line);
            out[line] = '\0';
            memmove(link->rbuf, nl + 1, link->rlen - line - 1);
            link->rlen -= line + 1;
            *response = out;
            return 0;
        }
        if (link->rlen == link->rcap) {
            if (link->rcap >= EDL_MAX_REPLY)
                return -1;
            size_t cap = link->rcap ? link->rcap * 2 : 64 * 1024;
            char *grown = realloc(link->rbuf, cap);
            if (grown == NULL)
                return -1;
            link->rbuf = grown;
            link->rcap = cap;
        }
        struct pollfd pfd = { .fd = link->ctl_fd, .events = POLLIN };
        int rc = poll(&pfd, 1, timeout_ms);
        if (rc <= 0)
            return -1;
        ssize_t n = recv(link->ctl_fd, link->rbuf + link->rlen, link->rcap - link->rlen, 0);
        if (n <= 0)
            return -1;
        link->rlen += (size_t)n;
    }
}

int edl_connect(edl_link_t *link, const edl_session_t *session, char *err, size_t err_size)
{
    edl_close(link);
    link->ctl_fd = connect_spec(session->control, err, err_size);
    if (link->ctl_fd < 0)
        return -1;

    cJSON *req = cJSON_CreateObject();
    cJSON_AddStringToObject(req, "command", "hello");
    if (session->token[0] != '\0')
        cJSON_AddStringToObject(cJSON_AddObjectToObject(req, "params"), "token", session->token);
    char *line = cJSON_PrintUnformatted(req);
    cJSON_Delete(req);
    if (line == NULL) {
        snprintf(err, err_size, "out of memory");
        return -1;
    }

    char *resp = NULL;
    int rc = edl_call(link, line, &resp, 5000);
    free(line);
    if (rc != 0 || strstr(resp, "\"error\"") != NULL) {
        snprintf(err, err_size, "EtherDOG refused the connection: %.300s", rc ? "no reply" : resp);
        free(resp);
        edl_close(link);
        return -1;
    }
    free(resp);
    return 0;
}

/* --- data session ---------------------------------------------------------------------- */

static int parse_server(const char *spec, struct sockaddr_storage *out, socklen_t *len)
{
    memset(out, 0, sizeof(*out));
    if (strncmp(spec, "unix:", 5) == 0) {
        struct sockaddr_un *un = (struct sockaddr_un *)out;
        if (strlen(spec + 5) >= sizeof(un->sun_path))
            return -1;
        un->sun_family = AF_UNIX;
        snprintf(un->sun_path, sizeof(un->sun_path), "%s", spec + 5);
        *len = sizeof(*un);
        return 0;
    }
    if (strncmp(spec, "udp:", 4) == 0) {
        char host[64];
        const char *rest = spec + 4;
        const char *colon = strrchr(rest, ':');
        if (colon == NULL || (size_t)(colon - rest) >= sizeof(host))
            return -1;
        memcpy(host, rest, (size_t)(colon - rest));
        host[colon - rest] = '\0';
        struct sockaddr_in *in = (struct sockaddr_in *)out;
        in->sin_family = AF_INET;
        in->sin_port = htons((uint16_t)atoi(colon + 1));
        if (inet_pton(AF_INET, host, &in->sin_addr) != 1)
            return -1;
        *len = sizeof(*in);
        return 0;
    }
    return -1;
}

int edl_open_data(edl_link_t *link, const edl_session_t *session, const char *local_dir,
                  char *err, size_t err_size)
{
    char endpoint[160];
    if (link->data_fd >= 0) {
        close(link->data_fd);
        link->data_fd = -1;
    }
    if (link->data_path[0] != '\0') {
        unlink(link->data_path);
        link->data_path[0] = '\0';
    }

    if (session->udp) {
        link->data_fd = socket(AF_INET, SOCK_DGRAM, 0);
        struct sockaddr_in local;
        memset(&local, 0, sizeof(local));
        local.sin_family = AF_INET;
        local.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
        socklen_t len = sizeof(local);
        if (link->data_fd < 0 || bind(link->data_fd, (struct sockaddr *)&local, sizeof(local)) != 0 ||
            getsockname(link->data_fd, (struct sockaddr *)&local, &len) != 0) {
            snprintf(err, err_size, "cannot bind data socket: %s", strerror(errno));
            return -1;
        }
        snprintf(endpoint, sizeof(endpoint), "udp:127.0.0.1:%u", (unsigned)ntohs(local.sin_port));
    } else {
        struct sockaddr_un local;
        memset(&local, 0, sizeof(local));
        local.sun_family = AF_UNIX;
        int n = snprintf(local.sun_path, sizeof(local.sun_path), "%s/ethercat-client-%ld.sock",
                         local_dir, (long)getpid());
        if (n < 0 || (size_t)n >= sizeof(local.sun_path)) {
            snprintf(err, err_size, "data socket path too long");
            return -1;
        }
        unlink(local.sun_path);
        link->data_fd = socket(AF_UNIX, SOCK_DGRAM, 0);
        if (link->data_fd < 0 ||
            bind(link->data_fd, (struct sockaddr *)&local, sizeof(local)) != 0) {
            snprintf(err, err_size, "cannot bind %s: %s", local.sun_path, strerror(errno));
            return -1;
        }
        chmod(local.sun_path, 0600);
        snprintf(link->data_path, sizeof(link->data_path), "%s", local.sun_path);
        snprintf(endpoint, sizeof(endpoint), "unix:%s", local.sun_path);
    }

    cJSON *req = cJSON_CreateObject();
    cJSON_AddStringToObject(req, "command", "open_data");
    cJSON_AddStringToObject(cJSON_AddObjectToObject(req, "params"), "endpoint", endpoint);
    char *line = cJSON_PrintUnformatted(req);
    cJSON_Delete(req);
    char *resp = NULL;
    int rc = line ? edl_call(link, line, &resp, 5000) : -1;
    free(line);
    if (rc != 0) {
        snprintf(err, err_size, "no reply to open_data");
        return -1;
    }

    cJSON *root = cJSON_Parse(resp);
    const cJSON *e = root ? cJSON_GetObjectItemCaseSensitive(root, "error") : NULL;
    const cJSON *masters = root ? cJSON_GetObjectItemCaseSensitive(root, "masters") : NULL;
    if (cJSON_IsString(e) || !cJSON_IsArray(masters)) {
        snprintf(err, err_size, "open_data refused: %.300s", cJSON_IsString(e) ? e->valuestring : resp);
        cJSON_Delete(root);
        free(resp);
        return -1;
    }
    free(resp);
    memset(link->open, 0, sizeof(link->open));
    const cJSON *m;
    cJSON_ArrayForEach(m, masters)
    {
        const cJSON *idx = cJSON_GetObjectItemCaseSensitive(m, "index");
        const cJSON *ep = cJSON_GetObjectItemCaseSensitive(m, "endpoint");
        const cJSON *ses = cJSON_GetObjectItemCaseSensitive(m, "session");
        if (!cJSON_IsNumber(idx) || !cJSON_IsString(ep) || !cJSON_IsString(ses))
            continue;
        int i = idx->valueint;
        if (i < 0 || i >= EDL_MAX_MASTERS ||
            parse_server(ep->valuestring, &link->server[i], &link->server_len[i]) != 0)
            continue;
        link->session[i] = strtoull(ses->valuestring, NULL, 16);
        link->tx_seq[i] = 0;
        link->open[i] = true;
    }
    cJSON_Delete(root);
    return 0;
}

int edl_recv_inputs(edl_link_t *link, uint8_t *buf, size_t buf_size, int timeout_ms,
                    int *master_out, uint8_t *flags_out, const uint8_t **payload_out,
                    size_t *len_out)
{
    if (link->data_fd < 0)
        return -1;
    struct pollfd pfd = { .fd = link->data_fd, .events = POLLIN };
    int rc = poll(&pfd, 1, timeout_ms);
    if (rc == 0)
        return 0;
    if (rc < 0)
        return errno == EINTR ? 0 : -1;

    ssize_t n = recv(link->data_fd, buf, buf_size, 0);
    if (n < EDL_FRAME_HEADER)
        return 0;
    if (buf[0] != 'E' || buf[1] != 'D' || buf[2] != 'O' || buf[3] != 'G' ||
        buf[4] != PROTOCOL_VERSION || buf[5] != KIND_INPUTS)
        return 0;
    int master = buf[6];
    uint16_t len = (uint16_t)get_le(buf + 20, 2);
    if (master >= EDL_MAX_MASTERS || !link->open[master] ||
        get_le(buf + 8, 8) != link->session[master] || (size_t)n != EDL_FRAME_HEADER + (size_t)len)
        return 0;

    *master_out = master;
    *flags_out = buf[7];
    *payload_out = buf + EDL_FRAME_HEADER;
    *len_out = len;
    return 1;
}

int edl_send_outputs(edl_link_t *link, int master, const uint8_t *payload, size_t len, bool valid)
{
    if (link->data_fd < 0 || master < 0 || master >= EDL_MAX_MASTERS || !link->open[master] ||
        len > EDL_MAX_PAYLOAD)
        return -1;
    uint8_t frame[EDL_FRAME_HEADER + EDL_MAX_PAYLOAD];
    frame[0] = 'E';
    frame[1] = 'D';
    frame[2] = 'O';
    frame[3] = 'G';
    frame[4] = PROTOCOL_VERSION;
    frame[5] = KIND_OUTPUTS;
    frame[6] = (uint8_t)master;
    frame[7] = valid ? EDL_FLAG_VALID : 0;
    put_le(frame + 8, link->session[master], 8);
    put_le(frame + 16, ++link->tx_seq[master], 4);
    put_le(frame + 20, len, 2);
    put_le(frame + 22, 0, 2);
    if (len > 0)
        memcpy(frame + EDL_FRAME_HEADER, payload, len);
    ssize_t n = sendto(link->data_fd, frame, EDL_FRAME_HEADER + len, MSG_DONTWAIT,
                       (const struct sockaddr *)&link->server[master], link->server_len[master]);
    return n < 0 ? -1 : 0;
}

void edl_close(edl_link_t *link)
{
    if (link->data_fd >= 0)
        close(link->data_fd);
    if (link->data_path[0] != '\0')
        unlink(link->data_path);
    if (link->ctl_fd >= 0)
        close(link->ctl_fd);
    link->data_fd = -1;
    link->ctl_fd = -1;
    link->data_path[0] = '\0';
    free(link->rbuf);
    link->rbuf = NULL;
    link->rcap = 0;
    link->rlen = 0;
    memset(link->open, 0, sizeof(link->open));
}
