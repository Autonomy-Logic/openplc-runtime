# Troubleshooting

## Common Issues

### Installation Issues

#### Permission Denied

**Error:**
```
ERROR: This script must be run as root
```

**Solution:**
Run installation with sudo:
```bash
sudo ./install.sh
```

#### Build Directory Errors

**Error:**
```
ERROR: Failed to create build directory
```

**Solution:**
1. Check disk space: `df -h`
2. Verify write permissions: `ls -la`
3. Clean existing build: `rm -rf build/`
4. Run install again: `sudo ./install.sh`

#### CMake Configuration Failed

**Error:**
```
ERROR: CMake configuration failed
```

**Solution:**
1. Ensure CMake is installed: `cmake --version`
2. Check for missing dependencies: `sudo ./install.sh`
3. Review CMakeLists.txt for errors
4. Clean build directory and retry

#### Compilation Failed

**Error:**
```
ERROR: Compilation failed
```

**Solution:**
1. Check compiler installation: `gcc --version`
2. Verify all dependencies installed
3. Review error messages for missing headers
4. Check available memory: `free -h`
5. Try single-threaded build: Edit install.sh, change `make -j$(nproc)` to `make`

#### Python Virtual Environment Issues

**Error:**
```
ERROR: Failed to create virtual environment
```

**Solution:**
1. Ensure python3-venv installed: `sudo apt-get install python3-venv`
2. Check Python version: `python3 --version` (requires 3.8+)
3. Verify disk space available
4. Remove old venv: `rm -rf venvs/runtime/`
5. Run install again

---

### Runtime Issues

#### Cannot Start Runtime

**Error:**
```
ERROR: OpenPLC Runtime v4 is not installed.
```

**Solution:**
Run installation first:
```bash
sudo ./install.sh
sudo ./start_openplc.sh
```

#### Port Already in Use

**Error:**
```
OSError: [Errno 98] Address already in use
```

**Solution:**
1. Check what's using port 8443:
   ```bash
   sudo lsof -i :8443
   ```
2. Kill the process:
   ```bash
   sudo kill <PID>
   ```
3. Or use a different port (modify webserver/app.py)

#### Runtime Process Not Responding

**Error:**
```
No response from runtime
```

**Solution:**
1. Check if plc_main is running:
   ```bash
   ps aux | grep plc_main
   ```
2. Check runtime logs:
   ```bash
   curl -k https://localhost:8443/api/runtime-logs
   ```
3. Restart the runtime:
   ```bash
   sudo ./start_openplc.sh
   ```
4. Check for socket file:
   ```bash
   ls -la /run/runtime/plc_runtime.socket
   ```

#### Real-Time Scheduling Failed

**Error:**
```
Failed to set real-time priority
```

**Solution:**
1. Run with sudo (required for SCHED_FIFO)
2. Or grant capabilities:
   ```bash
   sudo setcap cap_sys_nice=+ep build/plc_main
   ```
3. Check system limits:
   ```bash
   ulimit -r
   ```

---

### Connection Issues

#### Cannot Connect from OpenPLC Editor

**Symptoms:**
- Editor shows "Connection refused"
- "Cannot connect to runtime"

**Solution:**
1. Verify runtime is running:
   ```bash
   ps aux | grep python3 | grep webserver
   ```
2. Check port binding:
   ```bash
   sudo netstat -tlnp | grep 8443
   ```
3. Test with curl:
   ```bash
   curl -k https://localhost:8443/api/ping
   ```
4. Check firewall rules:
   ```bash
   sudo ufw status
   sudo iptables -L
   ```

#### Certificate Issues

**Symptoms:**
- OpenPLC Editor shows certificate errors

**Solution:**
The OpenPLC Editor handles self-signed certificates automatically. If you're using the API directly with curl or other tools, use the `-k` flag to accept self-signed certificates.

#### Authentication Issues

**Symptoms:**
- Cannot create user or login from OpenPLC Editor
- "Invalid username or password"

**Solution:**
1. Check database exists:
   ```bash
   ls -la /var/run/runtime/restapi.db
   ```
2. Reset database (WARNING: deletes all users and reopens the first-user
   bootstrap window):
   ```bash
   sudo rm /var/run/runtime/restapi.db
   sudo ./start_openplc.sh
   ```
   The runtime holds a `bootstrap_marker` row inside `restapi.db` to prevent
   a second admin being created once the first exists. Clearing individual
   tables will leave this sentinel in place and keep bootstrap closed;
   full recovery requires removing the entire `restapi.db` file.
3. Check .env file exists:
   ```bash
   ls -la /var/run/runtime/.env
   ```

---

### Compilation Issues

#### Upload Failed

**Error:**
```
UploadFileFail: Uploaded ZIP file failed safety checks
```

**Solution:**
1. Verify ZIP file is valid: `unzip -t program.zip`
2. Check file size (must be <10 MB per file, <50 MB total)
3. Ensure no disallowed extensions (.exe, .dll, .sh, .bat, .js, .vbs, .scr)
4. Check for path traversal attempts (no `..` in paths)
5. Regenerate ZIP from OpenPLC Editor

#### Compilation Failed

**Error:**
```
CompilationStatus: FAILED
```

**Solution:**
1. Check compilation logs:
   ```bash
   curl -k https://localhost:8443/api/compilation-status
   ```
2. Verify all required files in ZIP:
   - Config0.c
   - Res0.c
   - debug.c
   - glueVars.c
   - c_blocks_code.cpp
   - lib/ directory
3. Check for syntax errors in custom C/C++ code
4. Verify OpenPLC Editor generated files correctly

#### Missing Source Files

**Error:**
```
[ERROR] Missing required source files
```

**Solution:**
1. Ensure ZIP contains all files from OpenPLC Editor
2. Check ZIP structure (should not have nested folders)
3. Regenerate program in OpenPLC Editor
4. Verify LOCATED_VARIABLES.h exists

#### Library Loading Failed

**Error:**
```
Failed to load PLC program
```

**Solution:**
1. Check library exists:
   ```bash
   ls -la build/libplc_*.so
   ```
2. Verify library is valid:
   ```bash
   file build/libplc_*.so
   ldd build/libplc_*.so
   ```
3. Check for missing dependencies
4. Review runtime logs for dlopen errors

---

### Docker Issues

#### Container Won't Start

**Solution:**
1. Check logs:
   ```bash
   docker logs openplc-runtime
   ```
2. Verify port not in use:
   ```bash
   sudo lsof -i :8443
   ```
3. Check volume permissions:
   ```bash
   docker volume inspect openplc-runtime-data
   ```
4. Try removing and recreating:
   ```bash
   docker rm -f openplc-runtime
   docker run -d --name openplc-runtime -p 8443:8443 -v openplc-runtime-data:/var/run/runtime ghcr.io/autonomy-logic/openplc-runtime:latest
   ```

#### Cannot Access Container

**Solution:**
1. Verify container is running:
   ```bash
   docker ps | grep openplc-runtime
   ```
2. Check port mapping:
   ```bash
   docker port openplc-runtime
   ```
3. Test from inside container:
   ```bash
   docker exec openplc-runtime curl -k https://localhost:8443/api/ping
   ```

#### Volume Permission Errors

**Solution:**
Fix permissions:
```bash
docker run --rm -v openplc-runtime-data:/data alpine chown -R 1000:1000 /data
```

#### Real-Time Performance Issues

**Solution:**
Use privileged mode or add capabilities:
```bash
docker run -d --name openplc-runtime --privileged -p 8443:8443 -v openplc-runtime-data:/var/run/runtime ghcr.io/autonomy-logic/openplc-runtime:latest
```

Or:
```bash
docker run -d --name openplc-runtime --cap-add=SYS_NICE -p 8443:8443 -v openplc-runtime-data:/var/run/runtime ghcr.io/autonomy-logic/openplc-runtime:latest
```

---

### Plugin Issues

#### Plugin Not Loading

**Solution:**
1. Check plugins.conf syntax
2. Verify plugin file exists
3. Check plugin venv created:
   ```bash
   ls -la venvs/
   ```
4. Review runtime logs for plugin errors
5. Test plugin manually:
   ```bash
   source venvs/plugin_name/bin/activate
   python3 core/src/drivers/plugins/python/plugin_name/plugin.py
   ```

#### Plugin Dependencies Missing

**Error:**
```
ModuleNotFoundError: No module named 'xxx'
```

**Solution:**
1. Check requirements.txt exists
2. Create/update plugin venv:
   ```bash
   sudo bash scripts/manage_plugin_venvs.sh create plugin_name
   ```
3. Install dependencies:
   ```bash
   sudo bash scripts/manage_plugin_venvs.sh install plugin_name
   ```
4. Verify installation:
   ```bash
   sudo bash scripts/manage_plugin_venvs.sh info plugin_name
   ```

#### Plugin Configuration Error

**Solution:**
1. Verify config file path in plugins.conf
2. Check JSON syntax in config file
3. Ensure config file readable
4. Review plugin documentation

---

### Debug Interface Issues

#### WebSocket Connection Failed

**Error:**
```
WebSocket connection failed
```

**Solution:**
1. Obtain JWT token first via login endpoint
2. Verify token in connection:
   ```javascript
   socket.io('wss://localhost:8443/api/debug', {
     query: { token: 'your_token_here' }
   })
   ```
3. Check token expiration
4. Verify WebSocket support in client

#### No Debug Response

**Solution:**
1. Check PLC is running:
   ```bash
   curl -k https://localhost:8443/api/status
   ```
2. Verify command format (hex string with spaces)
3. Check runtime logs for errors
4. Test with simple command (MD5):
   ```
   45 DE AD 00 00
   ```

#### Invalid Variable Index

**Error:**
```
Variable index out of range
```

**Solution:**
1. Get variable count with DEBUG_INFO (0x41)
2. Verify indexes are within range
3. Check PLC program has variables defined
4. Ensure program is loaded and initialized

---

### Performance Issues

#### High CPU Usage

**Solution:**
1. Check scan cycle time (may be too fast)
2. Review PLC program complexity
3. Optimize plugin operations
4. Monitor with:
   ```bash
   top -p $(pgrep plc_main)
   ```

#### Scan Cycle Overruns

**Symptoms:**
- Logs show "Overruns: X"
- Timing statistics show high latency

**Solution:**
1. Increase scan cycle time in PLC program
2. Optimize PLC logic
3. Reduce plugin I/O operations
4. Check system load:
   ```bash
   uptime
   vmstat 1
   ```
5. Ensure real-time scheduling active

#### Memory Leaks

**Symptoms:**
- Memory usage grows over time
- System becomes slow

**Solution:**
1. Monitor memory:
   ```bash
   watch -n 1 'ps aux | grep plc_main'
   ```
2. Check for plugin memory leaks
3. Review custom C/C++ code
4. Restart runtime periodically
5. Report issue with reproduction steps

---

### Network Issues

#### Cannot Connect from Remote Host

**Solution:**
1. Verify firewall allows port 8443:
   ```bash
   sudo ufw allow 8443/tcp
   ```
2. Check binding address in app.py (should be 0.0.0.0)
3. Verify network connectivity:
   ```bash
   ping <runtime-host>
   telnet <runtime-host> 8443
   ```
4. Check for NAT/routing issues

#### Slow Response Times

**Solution:**
1. Check network latency:
   ```bash
   ping <runtime-host>
   ```
2. Verify system load not high
3. Check for network congestion
4. Use WebSocket for debug (lower latency)
5. Optimize polling frequency

---

### Logging Issues

#### No Logs Appearing

**Solution:**
1. Check log socket exists:
   ```bash
   ls -la /run/runtime/log_runtime.socket
   ```
2. Verify runtime process running
3. Check log level setting
4. Test log retrieval:
   ```bash
   curl -k https://localhost:8443/api/runtime-logs
   ```

#### Logs Too Verbose

**Solution:**
1. Filter by level:
   ```bash
   curl -k "https://localhost:8443/api/runtime-logs?level=ERROR"
   ```
2. Adjust log level in code (LOG_LEVEL_INFO, LOG_LEVEL_WARNING, LOG_LEVEL_ERROR)
3. Implement log rotation

---

## Getting Help

### Collect Diagnostic Information

Before reporting issues, collect:

1. **System Information:**
   ```bash
   uname -a
   cat /etc/os-release
   ```

2. **Runtime Version:**
   ```bash
   git log -1 --oneline
   ```

3. **Runtime Logs:**
   ```bash
   curl -k https://localhost:8443/api/runtime-logs > runtime-logs.txt
   ```

4. **Compilation Logs:**
   ```bash
   curl -k https://localhost:8443/api/compilation-status > compilation-logs.txt
   ```

5. **Process Status:**
   ```bash
   ps aux | grep -E "(plc_main|python3.*webserver)" > process-status.txt
   ```

6. **Port Status:**
   ```bash
   sudo netstat -tlnp | grep 8443 > port-status.txt
   ```

### Report Issues

When reporting issues:

1. Describe the problem clearly
2. Include steps to reproduce
3. Attach diagnostic information
4. Specify your environment (OS, Docker, etc.)
5. Include relevant log excerpts
6. Mention any recent changes

### Community Support

- GitHub Issues: https://github.com/Autonomy-Logic/openplc-runtime/issues
- Documentation: https://docs.devin.ai

## Related Documentation

- [Editor Integration](EDITOR_INTEGRATION.md) - How OpenPLC Editor connects to runtime
- [Architecture](ARCHITECTURE.md) - System overview
- [API Reference](API.md) - REST endpoints
- [Docker Deployment](DOCKER.md) - Container issues
- [Security](SECURITY.md) - Security-related issues
- [Development](DEVELOPMENT.md) - Development setup
