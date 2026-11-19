FUNCTION_BLOCK Overview EXTENDS HmiScreen
  VAR
    Body : HmiHorizontalStack;
    RunSwitch : HmiSwitch := (Caption := 'Run', Parent := 'Body');
    RunLamp : HmiLamp := (Caption := 'Running', Parent := 'Body');
    LevelValue : HmiValue := (Caption := 'Level', Decimals := 1, Parent := 'Body');
    Leave : HmiButton := (Text := 'Sign out', Parent := 'Body');
    Limit : HmiSlider := (Caption := 'Setpoint', MaxValue := 100.0, Parent := 'Body');
    Entry : HmiNumericInput := (Caption := 'Setpoint', Decimals := 0, MinValue := 0.0, MaxValue := 100.0, Parent := 'Body');
  END_VAR
  RunSwitch.Value := instance0.Main.Run;
  RunLamp.Value := instance0.Main.Run;
  LevelValue.Value := instance0.Main.Level;
  Limit.Value := instance0.Main.Setpoint;
  Entry.Value := instance0.Main.Setpoint;
  IF Leave.Clicked THEN HMI_SignOut(); END_IF;
END_FUNCTION_BLOCK
