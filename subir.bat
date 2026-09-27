@echo off
cd /d "D:\Projetos\IPTV"

echo ===================================================
echo   ATUALIZANDO REPOSITORIO: FlyngTV (renatomil5Gmail)
echo ===================================================

"C:\Program Files\Git\cmd\git.exe" config user.name "renatomil5Gmail"
"C:\Program Files\Git\cmd\git.exe" config user.email "renatomil5@gmail.com"

"C:\Program Files\Git\cmd\git.exe" add .

set /p mensagem="Digite o que foi alterado: "

"C:\Program Files\Git\cmd\git.exe" commit -m "%mensagem%"

"C:\Program Files\Git\cmd\git.exe" push https://github.com/renatomil5Gmail/FlyngTV.git main

echo ===================================================
echo   ENVIO CONCLUIDO PARA FlyngTV!
echo ===================================================
pause