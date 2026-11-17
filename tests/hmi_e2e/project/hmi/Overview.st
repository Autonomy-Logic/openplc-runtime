FUNCTION_BLOCK Overview EXTENDS HmiScreen
  VAR
    Body : HmiHorizontalStack;
    RunSwitch : HmiSwitch := (Caption := 'Run', Parent := 'Body');
    RunLamp : HmiLamp := (Caption := 'Running', Parent := 'Body');
    LevelValue : HmiValue := (Caption := 'Level', Decimals := 1, Parent := 'Body');
    Leave : HmiButton := (Text := 'Sign out', Parent := 'Body');
  END_VAR
  RunSwitch.Value := instance0.Main.Run;
  RunLamp.Value := instance0.Main.Run;
  LevelValue.Value := instance0.Main.Level;
  IF Leave.Clicked THEN HMI_SignOut(); END_IF;
END_FUNCTION_BLOCK
