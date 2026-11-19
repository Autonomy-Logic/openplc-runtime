FUNCTION_BLOCK Status EXTENDS HmiScreen
  VAR
    Body : HmiHorizontalStack;
    RunLamp : HmiLamp := (Caption := 'Running', Parent := 'Body');
    LevelValue : HmiValue := (Caption := 'Level', Decimals := 1, Parent := 'Body');
  END_VAR
  RunLamp.Value := instance0.Main.Run;
  LevelValue.Value := instance0.Main.Level;
END_FUNCTION_BLOCK
