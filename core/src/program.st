VAR
	tempAtual : real := 0.0;
	botaoLigar : bool := FALSE;
	resistenciaON : bool := FALSE;
	alarmeON : bool := FALSE;
	ledStatus : bool := FALSE;
	tempSetpoint : real := 200.0;
	tempMaxima : real := 250.0;
	histerese : real := 5.0;
	contadorCiclos : int := 0;
	tempMedia : real := 0.0;
	acumulador : real := 0.0;
	i : int := 0;
	numAmostras : int := 5;
	mensagem : string := '';
END_VAR

