on run
	set localPort to "5909"
	set tunnelCheck to "/usr/bin/nc -z 127.0.0.1 " & localPort & " >/dev/null 2>&1"

	try
		do shell script tunnelCheck
	on error
		try
			do shell script "/bin/launchctl kickstart -k gui/$(/usr/bin/id -u)/com.tontiancloud.astraquote-desktop-tunnel"
		on error errorMessage
			display alert "AstraQuote 运维桌面连接失败" message errorMessage as critical
			return
		end try

		set tunnelReady to false
		repeat 15 times
			delay 1
			try
				do shell script tunnelCheck
				set tunnelReady to true
				exit repeat
			end try
		end repeat

		if tunnelReady is false then
			display alert "AstraQuote 运维桌面连接超时" message "后台安全通道未能在 15 秒内建立，请稍后再试。" as critical
			return
		end if
	end try

	delay 1
	try
		set vncPassword to do shell script "/usr/bin/security find-generic-password -a ec2-user -s 'AstraQuote VNC' -w"
		open location "vnc://:" & vncPassword & "@localhost:" & localPort
	on error errorMessage
		display alert "AstraQuote 运维桌面自动登录配置缺失" message errorMessage as critical
	end try
end run
