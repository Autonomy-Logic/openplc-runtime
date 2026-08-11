#!/bin/bash
# Builds the runtime, compiles a real PLC program, builds the test fixtures, and
# runs the finding-by-finding suite. Single runtime instance at a time: the
# command socket is single-client, so no webserver competes for it.
set -uo pipefail

echo "### build runtime"
mkdir -p /work
(cd /src && tar cf - --exclude=build --exclude=venvs --exclude=.git --exclude=core/generated .) | (cd /work && tar xf -)
cp -r /workdir/venvs /work/venvs
cd /work && mkdir -p build && cd build && cmake .. >/tmp/cmake.log 2>&1 && make -j"$(nproc)" >/tmp/make.log 2>&1 \
  || { echo "BUILD FAIL"; tail -25 /tmp/make.log; exit 1; }
cd /work
echo "  warnings: $(grep -ci warning /tmp/make.log)"

echo "### build fixtures"
mkdir -p build/plugins
gcc -shared -fPIC -O1 -Wall -o build/plugins/libfakevpp_plugin.so \
    /fixtures/fakevpp_plugin.c -I core/src/drivers -I "$(python3 -c 'import sysconfig;print(sysconfig.get_paths()["include"])')" \
    -lpthread 2>&1 | head -5 || { echo "FAKEVPP BUILD FAIL"; exit 1; }
gcc -shared -fPIC -O1 -Wall -o /tmp/failinject.so /fixtures/failinject.c -ldl 2>&1 | head -5
echo '{"name":"fakevpp","protocol":"NONE","config":{}}' > /tmp/fakevpp_config.json
ls -la build/plugins/libfakevpp_plugin.so /tmp/failinject.so | sed 's/^/  /'

echo "### compile a real PLC program (through the webserver's own compile path)"
mkdir -p /run/runtime core/generated && cp -r /payload/. core/generated/
bash scripts/compile.sh >/tmp/compile.log 2>&1
ls build/libplc_*.so >/dev/null 2>&1 || {
  # compile.sh leaves the .so as new_libplc.so when invoked outside the webserver
  if [ -f build/new_libplc.so ]; then
    mv build/new_libplc.so "build/libplc_$(date +%s%N).so"
  else
    echo "COMPILE FAIL"; tail -12 /tmp/compile.log; exit 1
  fi
}
echo "  program: $(ls build/libplc_*.so)"

echo "### plugins.conf: the shipped set plus the fake VPP"
# The stock Python entries stay, disabled, because loading them is what
# initialises the interpreter -- and has_python_plugin && Py_IsInitialized() is
# the precondition for the Py_FinalizeEx() shutdown crash. A conf with only the
# native fixture in it quietly makes that whole class untestable.
# Native plugin lines whose .so is not built are dropped: they only add warnings.
grep -v 'libs7comm_plugin\|libethercat_plugin' plugins_default.conf > plugins.conf
# name,path,enabled,type,config_json,venv   (type 1 = native)
printf 'fakevpp,./build/plugins/libfakevpp_plugin.so,1,1,/tmp/fakevpp_config.json,\n' >> plugins.conf
sed 's/^/  /' plugins.conf

echo "### start the log-socket server (so --print-logs reaches stdout)"
python3 /fixtures/logserver.py &
LOGSRV=$!
sleep 0.4

/work/venvs/runtime/bin/python /fixtures/suite.py
RC=$?
kill $LOGSRV 2>/dev/null
exit $RC
